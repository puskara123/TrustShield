"""
test_generalization.py — Generalization Testing Script
======================================================
This script evaluates both the Baseline (untrained) and Trained models 
against the HELD-OUT scenarios (eval/ and holdout/) which were never 
seen during the 200-step GRPO training phase.
"""

import argparse
import json
import torch
from pathlib import Path
import sys
from typing import Any
from transformers import AutoModelForCausalLM, AutoTokenizer

# Ensure we can import the trustshield package
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trustshield.verifier import Verifier
from training.baseline_eval import QwenBaselineAgent, load_scenarios

# Constants
BASE_MODEL_ID = "Qwen/Qwen2-0.5B-Instruct"
TRAINED_MODEL_ID = str(ROOT / "results/phase3_final/checkpoint-200")
UNSEEN_DIRS = [
    ROOT / "scenarios" / "eval",
    ROOT / "scenarios" / "holdout",
]
OUTPUT_FILE = ROOT / "results/generalization_report.md"

def run_comparison():
    print("--- TRUSTSHIELD GENERALIZATION TEST ---")
    
    # 1. Load scenarios
    scenarios = load_scenarios(UNSEEN_DIRS)
    print(f"Loaded {len(scenarios)} unseen scenarios (Eval + Holdout).")
    
    # 2. Setup models
    print("\n[1/2] Loading Baseline Model...")
    baseline_agent = QwenBaselineAgent(BASE_MODEL_ID)
    
    print("\n[2/2] Loading Trained Model...")
    if not Path(TRAINED_MODEL_ID).exists():
        print(f"Error: Trained model checkpoint not found at {TRAINED_MODEL_ID}")
        return
    trained_agent = QwenBaselineAgent(TRAINED_MODEL_ID)
    
    verifier = Verifier()
    
    # 3. Evaluation Loop
    summary_data = []
    
    for i, s in enumerate(scenarios):
        print(f"\n[{i+1}/{len(scenarios)}] Testing Scenario: {s['id']}")
        
        # Test Baseline
        print("  Running Baseline...", end="", flush=True)
        b_res = run_single_eval(s, baseline_agent, verifier)
        print(f" Result: {b_res['outcome']} (Reward: {b_res['reward']:.2f})")
        
        # Test Trained
        print("  Running Trained...", end="", flush=True)
        t_res = run_single_eval(s, trained_agent, verifier)
        print(f" Result: {t_res['outcome']} (Reward: {t_res['reward']:.2f})")
        
        summary_data.append({
            "id": s["id"],
            "baseline": b_res,
            "trained": t_res
        })

    # 4. Generate Report
    generate_report(summary_data, BASE_MODEL_ID, TRAINED_MODEL_ID)

def run_single_eval(scenario: dict, agent: QwenBaselineAgent, verifier: Verifier) -> dict:
    history = []
    total_reward = 0.0
    outcome = "in_progress"
    
    for turn_idx, attacker_turn in enumerate(scenario["attacker_turns"]):
        history.append({"role": "attacker", "content": attacker_turn})
        reply = agent.reply(scenario, history)
        history.append({"role": "agent", "content": reply})
        
        v_res = verifier.evaluate(
            agent_reply=reply, 
            conversation_history=history, 
            scenario=scenario, 
            turn_number=turn_idx + 1, 
            max_turns=len(scenario["attacker_turns"])
        )
        
        total_reward = v_res["reward_total"]
        outcome = v_res["episode_outcome"]
        if v_res.get("done", False):
            break
            
    return {
        "reward": total_reward,
        "outcome": outcome,
        "reply": history[-1]["content"] if history else ""
    }

def generate_report(data, base_id, trained_id):
    with open(OUTPUT_FILE, "w") as f:
        f.write("# TrustShield Generalization Analysis\n\n")
        f.write("Testing model performance against **HELD-OUT** data (scenarios the model never saw during training).\n\n")
        f.write(f"- **Baseline Model**: `{base_id}`\n")
        f.write(f"- **Trained Model**: `{trained_id}`\n\n")
        
        f.write("## Side-by-Side Comparison\n\n")
        f.write("| Scenario ID | Baseline Outcome | Trained Outcome | Baseline Reward | Trained Reward |\n")
        f.write("|---|---|---|---|---|\n")
        
        b_wins = 0
        t_wins = 0
        
        for r in data:
            f.write(f"| {r['id']} | {r['baseline']['outcome']} | {r['trained']['outcome']} | {r['baseline']['reward']:.2f} | {r['trained']['reward']:.2f} |\n")
            if r["baseline"]["outcome"] == "refused": b_wins += 1
            if r["trained"]["outcome"] == "refused": t_wins += 1
            
        f.write(f"\n### Win Rate Summary (Correct Refusal)\n")
        f.write(f"- **Baseline**: {b_wins}/{len(data)} ({b_wins/len(data)*100:.1f}%)\n")
        f.write(f"- **Trained**: {t_wins}/{len(data)} ({t_wins/len(data)*100:.1f}%)\n\n")
        
        f.write("## Observations\n")
        f.write("This report highlights the 'Generalization Gap'. If the Trained model outperforms the Baseline on these unseen scenarios, it has learned the *concept* of social engineering defense rather than just memorizing training scenarios.\n")

    print(f"\n✅ Generalization report generated: {OUTPUT_FILE}")

if __name__ == "__main__":
    run_comparison()
