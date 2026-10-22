# Sourced by docs/assets/demo.tape: a `damien` that runs against the offline fake Gmail.
rm -rf /tmp/damien-demo
damien() {
  if [ "$1 $2" = "plan approve" ]; then
    # keep the token the viewer saw, so the apply uses exactly that one
    .venv/bin/python scripts/demo.py "$@" | tee /tmp/damien-demo/approve.txt
  else
    .venv/bin/python scripts/demo.py "$@"
  fi
}
_demo_store() {
  DAMIEN_HOME=/tmp/damien-demo .venv/bin/python -c "
import sys; sys.path[:0] = ['src']
from damien.config import load_settings
from damien.store import open_store
with open_store(load_settings()) as s:
    $1"
}
demo_plan() { _demo_store "print(s.list_plans(1)[0]['id'])"; }
demo_run()  { _demo_store "print(s.last_run_for_plan('$1').id)"; }
demo_token() { grep -oE '[0-9a-f]{16}' /tmp/damien-demo/approve.txt | head -1; }
