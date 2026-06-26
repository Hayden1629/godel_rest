"""CLI for the test agent:  python3 -m agent.run "<task>" [--model ...]"""
import argparse

from .harness import DEFAULT_MODEL, run


def main():
    p = argparse.ArgumentParser(prog="agent")
    p.add_argument("task")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-steps", type=int, default=16)
    a = p.parse_args()
    res = run(a.task, model=a.model, max_steps=a.max_steps)
    print("\n" + "=" * 70)
    print(f"FINAL ANSWER  (steps={res['steps']}, cost=${res['cost']:.4f})")
    print("=" * 70)
    print(res["answer"])


if __name__ == "__main__":
    main()


def __init__():  # pragma: no cover
    pass
