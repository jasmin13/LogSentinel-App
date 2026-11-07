import os
import re
import time
import json
import threading
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="LogSentinel Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GGUF_PATH = os.path.join(os.path.dirname(__file__), "logsentinel.f16.gguf")
MODEL_DIR = os.path.join(os.path.dirname(__file__), "model")

engine_type = "loading"
llm = None
hf_model = None
hf_tokenizer = None
is_loading = True
loading_error = None

def load_engine_background():
    global engine_type, llm, hf_model, hf_tokenizer, is_loading, loading_error
    t0 = time.time()
    
    # Priority 1: High-Speed C++ Edge Engine (GGUF via llama-cpp)
    try:
        from llama_cpp import Llama
        if os.path.exists(GGUF_PATH):
            print(f"Loading High-Speed GGUF Engine from {GGUF_PATH}...", flush=True)
            threads = min(os.cpu_count() or 4, 8)
            llm = Llama(
                model_path=GGUF_PATH,
                n_ctx=1024,
                n_threads=threads,
                verbose=False
            )
            engine_type = f"High-Speed Edge C++ ({threads} Threads)"
            is_loading = False
            print(f"LogSentinel Engine ready in {time.time() - t0:.2f}s!", flush=True)
            return
    except Exception as e:
        print(f"GGUF engine fallback: {e}", flush=True)
        
    # Priority 2: Standard PyTorch Fallback
    try:
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM
        target_dir = MODEL_DIR if os.path.exists(MODEL_DIR) else "merged_log_model"
        print(f"Loading PyTorch Engine from {target_dir}...", flush=True)
        torch.set_num_threads(min(os.cpu_count() or 4, 8))
        hf_tokenizer = AutoTokenizer.from_pretrained(target_dir)
        hf_model = AutoModelForCausalLM.from_pretrained(target_dir, dtype=torch.float32)
        hf_model.eval()
        engine_type = "PyTorch CPU Engine"
        is_loading = False
        print(f"LogSentinel PyTorch Engine ready in {time.time() - t0:.2f}s!", flush=True)
    except Exception as e:
        loading_error = str(e)
        is_loading = False
        print(f"Error loading model: {e}", flush=True)

@app.on_event("startup")
def startup_event():
    thread = threading.Thread(target=load_engine_background, daemon=True)
    thread.start()

PROMPT_TEMPLATE = """Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

### Instruction:
Analyze the system log entry and output a structured diagnostic JSON with keys: severity (CRITICAL/HIGH/WARNING/INFO), service, error_type, root_cause, and recommended_fix. Return valid JSON only.

### Input:
{text}

### Response:
"""

class DiagnosticRequest(BaseModel):
    log_text: str

def clean_and_parse_json(raw_text: str):
    """Safely extracts balanced JSON object and handles trailing commentary."""
    text = raw_text.strip()
    
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    
    # 1. Direct parse
    try:
        return json.loads(text)
    except Exception:
        pass
        
    # 2. Extract balanced JSON object
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            char = text[i]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i+1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            pass
                        break

    # 3. Auto-heal unclosed brackets (if cutoff)
    if start != -1:
        sub = text[start:]
        open_cur = sub.count("{")
        close_cur = sub.count("}")
        if open_cur > close_cur:
            sub += "}" * (open_cur - close_cur)
        try:
            return json.loads(sub)
        except Exception:
            pass

    raise ValueError("Model output could not be parsed into valid diagnostic JSON structure.")

@app.post("/api/diagnose")
def diagnose_log(req: DiagnosticRequest):
    if is_loading or (llm is None and hf_model is None):
        raise HTTPException(status_code=503, detail="AI engine is warming up (1-2s). Please retry.")
    
    if not req.log_text.strip():
        raise HTTPException(status_code=400, detail="Log content cannot be empty.")
    
    start_time = time.time()
    prompt = PROMPT_TEMPLATE.replace("{text}", req.log_text.strip())
    
    try:
        if llm is not None:
            output = llm(
                prompt,
                max_tokens=220,
                stop=["<|im_end|>", "\n###", "}\n\n"],
                temperature=0.0
            )
            raw_output = output["choices"][0]["text"].strip()
            tokens_gen = output["usage"]["completion_tokens"]
        else:
            import torch
            inputs = hf_tokenizer(prompt, return_tensors="pt")
            with torch.inference_mode():
                outputs = hf_model.generate(
                    **inputs,
                    max_new_tokens=220,
                    do_sample=False,
                    use_cache=True,
                    pad_token_id=hf_tokenizer.eos_token_id
                )
            raw_output = hf_tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True).strip()
            tokens_gen = len(outputs[0]) - inputs.input_ids.shape[1]

        latency_ms = round((time.time() - start_time) * 1000, 1)
        parsed_data = clean_and_parse_json(raw_output)
        
        return {
            "success": True,
            "data": parsed_data,
            "raw_output": raw_output,
            "latency_ms": latency_ms,
            "tokens_generated": tokens_gen,
            "engine": engine_type
        }
    except Exception as e:
        latency_ms = round((time.time() - start_time) * 1000, 1)
        return {
            "success": False,
            "error": str(e),
            "raw_output": raw_output if 'raw_output' in locals() else "",
            "latency_ms": latency_ms
        }

@app.get("/api/stats")
def get_stats():
    return {
        "status": "Online" if not is_loading else "Warming Up",
        "engine": engine_type,
        "security": "100% Offline / Zero External PII Leaks",
        "model_loaded": not is_loading and (llm is not None or hf_model is not None),
        "is_loading": is_loading
    }

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def serve_index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "LogSentinel API is running. UI not found."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8002, reload=False)
