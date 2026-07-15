import subprocess
import sys
import os

def run_step(cmd, description):
    print(f"\n{'='*60}")
    print(f"🚀 STEP: {description}")
    print(f"{'='*60}")
    try:
        # Run the command and wait for it to finish
        subprocess.run(cmd, check=True)
        print(f"\n✅ SUCCESS: {description} completed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ ERROR: {description} failed with exit code {e.returncode}.")
        print("Stopping pipeline.")
        sys.exit(1)
    except FileNotFoundError:
        print(f"\n❌ ERROR: Could not find the script for {description}. Are you in the root folder?")
        sys.exit(1)

def main():
    # Verify the script is running from the root folder
    if not os.path.exists("requirements.txt") or not os.path.exists("data-generator"):
        print("❌ ERROR: Please run this script from the root MulePredator directory.")
        sys.exit(1)

    # sys.executable ensures we use the Python from your active virtual environment
    python_exe = sys.executable 

    # --- 1. Install Requirements ---
    run_step(
        [python_exe, "-m", "pip", "install", "-r", "requirements.txt"], 
        "Installing Dependencies"
    )

    # --- 2. Generate Base Data ---
    run_step(
        [python_exe, "data-generator/generate.py"], 
        "Generating Synthetic Banking Data"
    )

    # --- 3. Build Feature Tables ---
    run_step(
        [
            python_exe, "data-generator/build_feature_table.py", 
            "--input-dir", "data/final", 
            "--output-dir", "data/features", 
            "--config", "config.yaml", 
            "--window", "6h"
        ], 
        "Building Feature Tables"
    )

    # --- 4. Run Graph Engine ---
    run_step(
        [
            python_exe, "engines/graph_engine.py", 
            "--features", "data/features/features.csv", 
            "--output-dir", "data/graph"
        ], 
        "Running Graph Engine"
    )

    # --- 5. Run Cyber Engine ---
    run_step(
        [
            python_exe, "engines/cyber_engine.py", 
            "--features", "data/features/features.csv", 
            "--output-dir", "data/cyber"
        ], 
        "Running Cyber Engine"
    )

    # --- 6. Run Quantum Engine ---
    run_step(
        [
            python_exe, "engines/quantum_engine.py", 
            "--features", "data/features/features.csv", 
            "--output-dir", "data/quantum"
        ], 
        "Running Quantum Engine"
    )

    # --- 7. Run Fusion Engine ---
    run_step(
        [python_exe, "engines/fusion_engine.py"], 
        "Running Fusion Engine (Convergence)"
    )

    print(f"\n{'*'*60}")
    print("🎉 ALL SETUP STEPS COMPLETED SUCCESSFULLY!")
    print("You are now ready to launch the FastAPI server and Replayer.")
    print(f"{'*'*60}\n")

if __name__ == "__main__":
    main()