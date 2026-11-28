"""Train the optional selector with previously sampled, verified work orders."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root), str(root / "src")]
    from foundation_training import main
    main()
