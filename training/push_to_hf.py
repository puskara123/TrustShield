import os
from huggingface_hub import login, HfApi
from transformers import AutoModelForCausalLM, AutoTokenizer
import argparse
import getpass

def resolve_cache_root() -> str:
    env_cache = os.environ.get("HF_CACHE_DIR")
    if env_cache:
        return env_cache
    default_cache = "/app/hf_cache"
    try:
        os.makedirs(default_cache, exist_ok=True)
        return default_cache
    except OSError:
        return os.path.expanduser("~/.cache/trustshield/hf_cache")

CACHE_ROOT = resolve_cache_root()
os.makedirs(CACHE_ROOT, exist_ok=True)
os.makedirs(os.path.join(CACHE_ROOT, "datasets"), exist_ok=True)
os.makedirs(os.path.join(CACHE_ROOT, "hub"), exist_ok=True)
os.environ["HF_HOME"] = CACHE_ROOT
os.environ["TRANSFORMERS_CACHE"] = os.path.join(CACHE_ROOT, "transformers")
os.environ["HF_DATASETS_CACHE"] = os.path.join(CACHE_ROOT, "datasets")
os.environ["HUGGINGFACE_HUB_CACHE"] = os.path.join(CACHE_ROOT, "hub")
os.environ["XDG_CACHE_HOME"] = CACHE_ROOT

def push(repo_id, folder_path, model_path=None, repo_type="model"):
    token = os.environ.get("HF_TOKEN")
    if not token:
        token = getpass.getpass("Enter Hugging Face token: ").strip()
    if not token:
        print("Error: Hugging Face token was not provided.")
        return

    print(f"Logging in to Hugging Face...")
    login(token=token)
    
    api = HfApi()
    
    # 1. Push model weights if provided (only for model repos)
    if model_path and os.path.exists(model_path):
        if repo_type == "model":
            print(f"Pushing model from {model_path} to {repo_id}...")
            model = AutoModelForCausalLM.from_pretrained(model_path)
            tokenizer = AutoTokenizer.from_pretrained(model_path)
            model.push_to_hub(repo_id)
            tokenizer.push_to_hub(repo_id)
        else:
            print(f"Uploading model folder {model_path} into Space repo {repo_id} at /model...")
            api.upload_folder(
                folder_path=model_path,
                path_in_repo="model",
                repo_id=repo_id,
                repo_type=repo_type
            )
    
    # 2. Push results/content folder
    if os.path.exists(folder_path):
        print(f"Pushing folder {folder_path} to {repo_id} ({repo_type})...")
        api.upload_folder(
            folder_path=folder_path,
            repo_id=repo_id,
            repo_type=repo_type
        )
    
    print(f"✅ Successfully pushed to https://huggingface.co/{repo_id} ({repo_type})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push model/results to Hugging Face Hub")
    parser.add_argument("--repo", required=True, help="HF Repo ID (e.g., username/repo)")
    parser.add_argument("--folder", default="./results", help="Folder to upload")
    parser.add_argument("--model", help="Path to model checkpoint to push")
    parser.add_argument(
        "--repo-type",
        default="model",
        choices=["model", "space"],
        help="Target repository type"
    )
    
    args = parser.parse_args()
    push(args.repo, args.folder, args.model, args.repo_type)
