import argparse
import json
from pathlib import Path

from .cards import CARD_DIR
from .run import run, summary_markdown

ap = argparse.ArgumentParser(prog="python -m forensic.validation", description=__doc__)
sub = ap.add_subparsers(dest="cmd", required=True)
r = sub.add_parser("run", help="run the validation corpus and write detector cards")
r.add_argument("--quick", action="store_true", help="small corpus (for CI)")
r.add_argument("--pristine", type=Path, help="folder of real camera images to derive test cases from")
r.add_argument("--out", type=Path, default=CARD_DIR, help="where to write the cards")
r.add_argument("--summary", type=Path, help="also write a Markdown summary here")
a = ap.parse_args()
out = run(quick=a.quick, pristine=a.pristine, out_dir=a.out)
print(json.dumps(out["results"], indent=2))
if a.summary:
    a.summary.parent.mkdir(parents=True, exist_ok=True)
    a.summary.write_text(summary_markdown(out))
