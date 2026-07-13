# Reference-agent example (Conversation-QA Foundation 1.3, item 11).
#
# `make reference` regenerates the 25 scenario + 25 conversation-test files and
# the suite, then runs the full 375-run suite OFFLINE through the deterministic
# simulator, records a browsable workspace, and prints the real counts + wall
# time. No live agent, no network, no model.

PY ?= python3
PYTHONPATH := ../../src

.PHONY: reference generate run serve clean

reference: run

generate:
	PYTHONPATH=$(PYTHONPATH) $(PY) generate.py

run: generate
	PYTHONPATH=$(PYTHONPATH) $(PY) run_reference.py

serve:
	PYTHONPATH=$(PYTHONPATH) $(PY) -m hotato serve --workspace reference --registry ./.workspace

clean:
	rm -rf ./.workspace ./.out
