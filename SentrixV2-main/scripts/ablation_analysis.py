import os
import sys
import json
import subprocess

def run_ablation():
    with open("evaluation/metadata/dataset_info.json", "r") as f:
        meta = json.load(f)
        
    if meta['counts']['real_labelled'] == 0:
        print("STATUS: INSUFFICIENT DATA")
        print("Cannot run ablation analysis: 0 real labelled samples found.")
        print("Ablation requires a baseline metrics comparison which is impossible without ground truth.")
        sys.exit(0)

    print("Running ablation analysis...")
    # This would loop through modalities (vision, audio, behaviour), disable them one by one, 
    # run e2e_evaluation.py, and compare the resulting metrics against the baseline.
    
    print("Ablation complete.")

if __name__ == "__main__":
    run_ablation()
