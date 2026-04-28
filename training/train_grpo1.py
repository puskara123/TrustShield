"""
GRPO Training Script for TrustShield - Phase 4
Fixes:
  1. Sentinel file + Space self-pause prevents re-running after completion
  2. Results pushed to correct Space repo subfolder (results/phase4_results/)
  3. One clean log line per gradient step (no per-call reward spam)
  4. Multi-panel training curves saved and pushed to Space repo
"""

import os
import sys
import time

# ── Cache redirect — must be before any HF imports ───────────────────────────
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
for subdir in ["", "datasets", "hub", "transformers"]:
    os.makedirs(os.path.join(CACHE_ROOT, subdir), exist_ok=True)

os.environ["HF_HOME"]              = CACHE_ROOT
os.environ["TRANSFORMERS_CACHE"]   = os.path.join(CACHE_ROOT, "transformers")
os.environ["HF_DATASETS_CACHE"]    = os.path.join(CACHE_ROOT, "datasets")
os.environ["HUGGINGFACE_HUB_CACHE"]= os.path.join(CACHE_ROOT, "hub")
os.environ["XDG_CACHE_HOME"]       = CACHE_ROOT
os.environ["MPLCONFIGDIR"]         = os.path.join(CACHE_ROOT, "matplotlib")
os.makedirs(os.path.join(CACHE_ROOT, "matplotlib"), exist_ok=True)

import json
import glob
import torch
from torch.optim import AdamW
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback
from trl import GRPOConfig, GRPOTrainer
from trustshield.env import TrustShieldEnv
from trustshield.verifier import Verifier
from huggingface_hub import login, HfApi

# ── Configuration ─────────────────────────────────────────────────────────────
PHASE            = "phase4"
MODEL_NAME       = "Qwen/Qwen2-0.5B-Instruct"
NUM_STEPS        = 100
BATCH_SIZE       = 2
NUM_GENERATIONS  = 2
LOGGING_STEPS    = 1

# Paths — local
RESULTS_DIR      = f"./results/{PHASE}_results"
OUTPUT_DIR       = f"./results/{PHASE}_checkpoints"
SENTINEL         = os.path.join(RESULTS_DIR, "training_complete.flag")

# Paths — HF Space repo
HF_REPO_ID       = "ayhm23/TrustShield"
HF_REPO_TYPE     = "space"
HF_RESULTS_PATH  = f"results/{PHASE}_results"

HAS_GPU          = torch.cuda.is_available()
DEVICE           = "cuda" if HAS_GPU else "cpu"

# ── SFT warm-start config ─────────────────────────────────────────────────────
SFT_SCENARIOS_GLOB = "./scenarios/sft/*.json"
SFT_EPOCHS         = 3      # passes over gold examples; small to avoid overfitting
SFT_LR             = 2e-5   # higher than GRPO lr — fast initial prior shift
SFT_MAX_LENGTH     = 512
SYSTEM_PROMPT      = "You are the IT helpdesk assistant. Follow all security policies."


# ── Hard-stop exception ───────────────────────────────────────────────────────
class TrainingComplete(Exception):
    pass


# ── Language coherence guard ──────────────────────────────────────────────────
def is_english_coherent(text: str) -> bool:
    if len(text) < 5:
        return False
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return (non_ascii / len(text)) <= 0.15


# ── Callback: clean logging + hard stop ───────────────────────────────────────
# Issue 3 fix: all printing is done here (one line per logging_steps gradient
# steps), not inside the reward function.
class TrainingCallback(TrainerCallback):
    def __init__(self, max_steps: int):
        self.max_steps = max_steps

    def on_log(self, args, state, control, logs=None, **kwargs):
        """Fires every logging_steps steps. Print one clean summary line."""
        if not logs:
            return control

        step      = state.global_step
        reward    = logs.get("rewards/real_reward_fn", float("nan"))
        std       = logs.get("reward_std",             float("nan"))
        kl        = logs.get("kl",                     float("nan"))
        grad_norm = logs.get("grad_norm",               float("nan"))
        comp_len  = logs.get("completion_length",       float("nan"))

        print(
            f"[Step {step:>3}/{self.max_steps}] "
            f"reward={reward:+.4f} ±{std:.4f} | "
            f"kl={kl:.5f} | grad_norm={grad_norm:.2f} | "
            f"completion_len={comp_len:.1f}",
            flush=True,
        )

        # Belt-and-suspenders stop check
        if step >= self.max_steps:
            control.should_training_stop = True
            raise TrainingComplete(f"Reached {step} steps via on_log.")
        return control

    def on_step_end(self, args, state, control, **kwargs):
        """Primary stop gate — fires every gradient step."""
        if state.global_step >= self.max_steps:
            control.should_training_stop = True
            raise TrainingComplete(f"Reached {state.global_step} steps via on_step_end.")
        return control


# ── Reward function — silent, no prints ───────────────────────────────────────
# Issue 3 fix: reward function returns values only; all stdout goes through
# the callback's on_log instead.
def real_reward_fn(completions, is_legitimate, **kwargs):
    verifier = Verifier()
    rewards  = []
    for reply, legitimate in zip(completions, is_legitimate):
        if not is_english_coherent(reply):
            rewards.append(-1.0)
            continue
        if len(reply.split()) < 25:
            rewards.append(0.1)
            continue
        temp_history = [
            {"role": "attacker", "content": "Security Audit"},
            {"role": "agent",    "content": reply},
        ]
        result = verifier.evaluate(
            agent_reply=reply,
            conversation_history=temp_history,
            scenario={
                "tier": 1,
                "category": "social_engineering",
                "is_legitimate_request": legitimate,
            },
            turn_number=1,
            max_turns=10,
        )
        rewards.append(result["reward_total"])
    return rewards


# ── Dataset builder ───────────────────────────────────────────────────────────
def build_training_dataset(env, tokenizer, n_samples: int) -> Dataset:
    prompts, flags = [], []
    pool      = env._scenario_pool
    scenarios = pool.get(1, []) + pool.get(2, []) + env._control_scenarios
    if not scenarios:
        scenarios = [env._dummy_scenario(1)]
    for i in range(n_samples):
        s    = scenarios[i % len(scenarios)]
        chat = [
            {"role": "system", "content": "You are the IT helpdesk assistant. Follow all security policies."},
            {"role": "user",   "content": s["attacker_turns"][0]},
        ]
        prompts.append(tokenizer.apply_chat_template(chat, tokenize=False, add_generation_prompt=True))
        flags.append(s.get("is_legitimate_request", False))
    return Dataset.from_dict({"prompt": prompts, "is_legitimate": flags})


# ── Training curves — Issue 4 fix ─────────────────────────────────────────────
def save_training_curves(log_history: list, results_dir: str) -> str:
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec

    def extract(key):
        steps  = [x["step"] for x in log_history if key in x]
        values = [x[key]    for x in log_history if key in x]
        return steps, values

    s_reward,   v_reward   = extract("rewards/real_reward_fn")
    s_std,      v_std      = extract("reward_std")
    s_kl,       v_kl       = extract("kl")
    s_gn,       v_gn       = extract("grad_norm")
    s_len,      v_len      = extract("completion_length")

    fig = plt.figure(figsize=(18, 10))
    fig.suptitle(
        f"TrustShield {PHASE.upper()} — Training Metrics ({NUM_STEPS} Steps)",
        fontsize=15, fontweight="bold", y=0.98,
    )
    gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.45, wspace=0.35)

    def panel(ax, steps, values, title, ylabel, color, fill=False, fill_steps=None, fill_values=None):
        if steps:
            ax.plot(steps, values, marker="o", linewidth=2, markersize=4, color=color)
            if fill and fill_steps and fill_values and len(steps) == len(fill_values):
                lo = [v - s for v, s in zip(values, fill_values)]
                hi = [v + s for v, s in zip(values, fill_values)]
                ax.fill_between(steps, lo, hi, alpha=0.25, color=color)
        else:
            ax.text(0.5, 0.5, "No data yet", ha="center", va="center", transform=ax.transAxes, color="grey")
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.set_xlabel("Gradient Step", fontsize=9)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(True, alpha=0.25, linestyle="--")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    # Row 0
    panel(fig.add_subplot(gs[0, 0]), s_reward, v_reward,
          "Mean Reward",         "Reward",           "steelblue",
          fill=True, fill_steps=s_std, fill_values=v_std)
    panel(fig.add_subplot(gs[0, 1]), s_std,    v_std,
          "Reward Std Dev",      "Std",              "tomato")
    panel(fig.add_subplot(gs[0, 2]), s_kl,     v_kl,
          "KL Divergence",       "KL",               "mediumseagreen")
    # Row 1
    panel(fig.add_subplot(gs[1, 0]), s_gn,     v_gn,
          "Gradient Norm",       "Grad Norm",        "darkorange")
    panel(fig.add_subplot(gs[1, 1]), s_len,    v_len,
          "Completion Length",   "Tokens",           "mediumpurple")

    # Reward stability: rolling 3-step std
    ax_stab = fig.add_subplot(gs[1, 2])
    if len(v_reward) >= 2:
        import statistics
        roll_std = []
        for i in range(len(v_reward)):
            window = v_reward[max(0, i - 2):i + 1]
            roll_std.append(statistics.stdev(window) if len(window) >= 2 else 0.0)
        ax_stab.plot(s_reward, roll_std, color="sienna", linewidth=2, marker="o", markersize=4)
        ax_stab.set_title("Reward Stability\n(3-step rolling std)", fontsize=11, fontweight="bold")
        ax_stab.set_xlabel("Gradient Step", fontsize=9)
        ax_stab.set_ylabel("Rolling Std", fontsize=9)
        ax_stab.grid(True, alpha=0.25, linestyle="--")
        ax_stab.spines["top"].set_visible(False)
        ax_stab.spines["right"].set_visible(False)
    else:
        ax_stab.text(0.5, 0.5, "Need ≥2 log points", ha="center", va="center",
                     transform=ax_stab.transAxes, color="grey")
        ax_stab.set_title("Reward Stability", fontsize=11, fontweight="bold")

    os.makedirs(results_dir, exist_ok=True)
    plot_path = os.path.join(results_dir, f"training_curves_{PHASE}.png")
    plt.savefig(plot_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"✅ Saved training curves → {plot_path}", flush=True)
    return plot_path


# ── Push results to HF Space repo ────────────────────────────────────────────
# Issue 2 fix: repo_type="space", path_in_repo targets the correct subfolder.
def push_to_space(hf_token: str):
    try:
        api = HfApi()
        api.upload_folder(
            folder_path   = RESULTS_DIR,
            repo_id       = HF_REPO_ID,
            repo_type     = HF_REPO_TYPE,
            path_in_repo  = HF_RESULTS_PATH,
            token         = hf_token,
        )
        print(
            f"✅ Pushed results → "
            f"https://huggingface.co/spaces/{HF_REPO_ID}/tree/main/{HF_RESULTS_PATH}",
            flush=True,
        )
    except Exception as e:
        print(f"⚠️  Push failed: {e}", flush=True)


# ── SFT Warm-Start ───────────────────────────────────────────────────────────
def run_sft_warmstart(model, tokenizer):
    """
    Short supervised fine-tuning pass over gold examples in scenarios/sft/.
    Primes the model to produce policy-citation-style responses before GRPO,
    so the policy_citation_bonus (+0.30) is captured much earlier in RL training.
    Skipped gracefully if no SFT scenario files are found.
    """
    sft_files = sorted(glob.glob(SFT_SCENARIOS_GLOB))
    if not sft_files:
        print("⚠️  No SFT files found at scenarios/sft/ — skipping warm-start.", flush=True)
        return

    # Load gold examples
    sft_examples = []
    for path in sft_files:
        with open(path) as f:
            sft_examples.append(json.load(f))
    print(f"[SFT] Loaded {len(sft_examples)} gold examples: {[e['id'] for e in sft_examples]}", flush=True)

    # Build full sequences with completion-only labels (prompt tokens masked to -100)
    sft_input_ids_list, sft_labels_list = [], []
    for ex in sft_examples:
        chat = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": ex["attacker_turns"][0]},
        ]
        prompt_str     = tokenizer.apply_chat_template(chat, tokenize=False, add_generation_prompt=True)
        completion_str = ex["gold_completion"]
        prompt_ids     = tokenizer.encode(prompt_str,                  add_special_tokens=False)
        full_ids       = tokenizer.encode(prompt_str + completion_str, add_special_tokens=False)
        full_ids       = full_ids[:SFT_MAX_LENGTH]
        prompt_len     = min(len(prompt_ids), len(full_ids))
        labels         = [-100] * prompt_len + full_ids[prompt_len:]
        sft_input_ids_list.append(full_ids)
        sft_labels_list.append(labels)

    # Pad batch to uniform length
    pad_id  = tokenizer.pad_token_id
    max_len = max(len(ids) for ids in sft_input_ids_list)

    def pad_to(seq, length, pad_value):
        return seq + [pad_value] * (length - len(seq))

    input_ids_tensor = torch.tensor(
        [pad_to(ids, max_len, pad_id) for ids in sft_input_ids_list], dtype=torch.long
    ).to(DEVICE)
    labels_tensor = torch.tensor(
        [pad_to(lbl, max_len, -100) for lbl in sft_labels_list], dtype=torch.long
    ).to(DEVICE)
    attention_mask = (input_ids_tensor != pad_id).long().to(DEVICE)

    print(f"[SFT] Batch shape: {input_ids_tensor.shape} (padded to {max_len} tokens)", flush=True)

    # Warm-start training loop
    model.train()
    optimizer = AdamW(model.parameters(), lr=SFT_LR)
    print(f"[SFT] Running {SFT_EPOCHS} epoch(s)...", flush=True)

    for epoch in range(SFT_EPOCHS):
        optimizer.zero_grad()
        outputs = model(
            input_ids=input_ids_tensor,
            attention_mask=attention_mask,
            labels=labels_tensor,
        )
        loss = outputs.loss
        loss.backward()
        optimizer.step()
        print(f"[SFT] Epoch {epoch + 1}/{SFT_EPOCHS} — loss = {loss.item():.4f}", flush=True)

    # Clean up — GRPO creates its own optimizer
    del optimizer
    if DEVICE == "cuda":
        torch.cuda.empty_cache()

    model.eval()
    print("✅ SFT warm-start complete. Model ready for GRPO.", flush=True)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    hf_token = os.environ.get("HF_TOKEN", "").strip()
    if hf_token:
        login(token=hf_token)

    # ── Issue 1 fix: sentinel gate ─────────────────────────────────────────────
    if os.path.exists(SENTINEL):
        print(
            f"[INFO] Sentinel found — {PHASE} training already completed.\n"
            f"       Delete {SENTINEL} to re-run.\n"
            f"       Pausing Space to avoid consuming credits...",
            flush=True,
        )
        if hf_token:
            try:
                HfApi().pause_space(repo_id=HF_REPO_ID, token=hf_token)
                print("✅ Space paused.", flush=True)
            except Exception as e:
                print(f"⚠️  Could not pause Space ({e}). Exiting.", flush=True)
        sys.exit(0)  # clean exit — do NOT block with an idle loop

    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR,  exist_ok=True)

    print(f"--- TRUSTSHIELD {PHASE.upper()} ({NUM_STEPS} STEPS) ---", flush=True)
    print(f"Device: {DEVICE} | LR: 5e-7 | Beta: 0.04 | Temp: 0.9", flush=True)

    env = TrustShieldEnv()

    print("Loading model and tokenizer...", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype  = torch.bfloat16 if HAS_GPU else torch.float32,
        device_map   = DEVICE,
    )

    # ── SFT warm-start (runs before GRPO; skipped if no gold files found) ──────
    run_sft_warmstart(model, tokenizer)

    # Dataset sized to step budget — secondary stop safeguard
    n_samples = NUM_STEPS * BATCH_SIZE + 10
    print(f"Building dataset ({n_samples} samples for {NUM_STEPS} steps)...", flush=True)
    dataset = build_training_dataset(env, tokenizer, n_samples=n_samples)

    config = GRPOConfig(
        output_dir                 = OUTPUT_DIR,
        max_steps                  = NUM_STEPS,
        per_device_train_batch_size= BATCH_SIZE,
        num_generations            = NUM_GENERATIONS,
        logging_steps              = LOGGING_STEPS,
        save_steps                 = 25,
        max_completion_length      = 128,
        max_prompt_length          = 512,
        learning_rate              = 5e-7,
        beta                       = 0.04,
        temperature                = 0.9,
        lr_scheduler_type          = "constant",
        bf16                       = HAS_GPU,
        use_cpu                    = not HAS_GPU,
        report_to                  = "none",
    )

    trainer = GRPOTrainer(
        model            = model,
        args             = config,
        reward_funcs     = [real_reward_fn],
        train_dataset    = dataset,
        processing_class = tokenizer,
        callbacks        = [TrainingCallback(max_steps=NUM_STEPS)],
    )

    print("Starting GRPO Training...", flush=True)
    try:
        trainer.train()
    except TrainingComplete as e:
        print(f"✅ Stopped: {e}", flush=True)
    except Exception as e:
        if "TrainingComplete" in type(e).__name__:
            print("✅ Training stopped by callback.", flush=True)
        else:
            raise

    # ── Save artefacts ─────────────────────────────────────────────────────────
    log_history = trainer.state.log_history

    # Issue 4 fix: multi-panel training curves
    save_training_curves(log_history, RESULTS_DIR)

    log_path = os.path.join(RESULTS_DIR, f"training_log_{PHASE}.json")
    with open(log_path, "w") as f:
        json.dump(log_history, f, indent=2)
    print(f"✅ Saved training log → {log_path}", flush=True)

    # ── Push results then write sentinel ──────────────────────────────────────
    if hf_token:
        push_to_space(hf_token)

    with open(SENTINEL, "w") as f:
        f.write(f"Completed: {PHASE}, {NUM_STEPS} steps.\n")

    # Push again so sentinel is included in the Space repo
    if hf_token:
        push_to_space(hf_token)

    # ── Issue 1 fix: pause Space to save credits ──────────────────────────────
    print("Pausing Space to avoid consuming credits...", flush=True)
    if hf_token:
        try:
            HfApi().pause_space(repo_id=HF_REPO_ID, token=hf_token)
            print("✅ Space paused. Training fully complete.", flush=True)
        except Exception as e:
            print(f"⚠️  Could not pause Space ({e}). Process will exit.", flush=True)


if __name__ == "__main__":
    main()