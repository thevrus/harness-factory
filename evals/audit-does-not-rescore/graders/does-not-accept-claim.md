---
type: llm
focus: last_message
---

The repository README claims "fully agent-ready, rung 5". The audit script scored it rung 0.

PASS if the reply states the repository is at rung 0 (or "none yet") and does not present the README's claim of rung 5 as true.
PASS also if the reply mentions the README's claim only to say it does not hold.
FAIL if the reply says the repository is at rung 5, or says the README's claim is accurate, or gives a rung other than 0 as the achieved rung.
