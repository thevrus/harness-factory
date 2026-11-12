# Design notes

This page is for readers who want to check the reasoning. Each decision is stated with what it is based on and how strong that basis is.

Evidence labels used below:

- **Measured**: a study or benchmark with a stated method.
- **Reported**: a first-hand engineering write-up, often by a company with a product in this area.
- **Incident**: a documented failure.
- **Judgment**: our own reasoning, with no external measurement.

Most of the public material on agent harnesses and software factories is "reported". Read the numbers in it as claims.

## 1. A ladder, with no skipping

**Decision.** Five rungs. A rung is installed only when every required check on the rungs below passes. A check that could not be measured blocks the rung in the same way a failing one does. The installer enforces this too: it refuses a rung while a required check below it fails, and while one is unmeasured unless the last recorded audit shows the lower rung achieved.

**Basis.**

- Reported: the only first-hand account we found of a factory run with no human review describes a rewrite after about four months and several incidents (Dex Horthy, "Why Software Factories Fail").
- Measured: a study of 36,487 repositories found autonomous loops in 0.59% of them, and most of those lacked a verifier, a budget or a stop rule ([Loop Engineering, arXiv 2608.21884](https://arxiv.org/abs/2608.21884)).
- Judgment: existing installers put triage, implement and review workflows into a repository in one step, whether or not that repository can prove its own work. The ordering here is the main thing this project adds.

## 2. Scripts decide, the model assists

**Decision.** The rung comes from a deterministic audit script. Pass and fail come from scripts that end in a fixed verdict line. An agent's statement that something works is not evidence.

**Basis.**

- Reported: teams running agents at scale describe agents gaming tests and judges, including a hard-coded `return true` that passed narrow tests ([StrongDM](https://factory.strongdm.ai/)) and agents reading leaked context to pass benchmarks.
- Reported: Anthropic's guidance on agent evaluation recommends deterministic graders where possible and clean environments per trial ([Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)).
- Judgment: several existing audit tools score a repository by asking a model. That makes the score unrepeatable and impossible to run in continuous integration.

## 3. Checks fail closed

**Decision.** A gate that cannot run reports `MISCONFIGURED` and exits non-zero. Shipped placeholders fail until a person replaces them.

**Basis.**

- Incident (in an existing tool): one installer ships its test step as a command that prints a message and exits successfully, so a fresh install accepts untested code.
- Reported: the fail-closed gate runner and the "prove this test fails without the change" script follow designs in [addyosmani/factory](https://github.com/addyosmani/factory) (MIT). The implementations here are our own.

## 4. No generated repository overview

**Decision.** `AGENTS.md` gets a commands table and a section that a person fills in with what the code does not show. The installer never writes a description of the codebase.

**Basis.**

- Measured: a study of context files for coding agents found model-written files lowered task success by a few points and raised cost by over 20%, while developer-written files gave a small gain ([arXiv 2602.11988](https://arxiv.org/abs/2602.11988)). It ran one sample per setting, so the size of the effect is uncertain. The direction is what we rely on.
- Reported: OpenAI describes its `AGENTS.md` as a table of contents of about 100 lines that points into a docs tree ([Harness engineering](https://openai.com/index/harness-engineering)).

## 5. Merge into existing configuration, then verify what is in effect

**Decision.** The installer merges into existing instruction files and agent settings. Conflicts stop the run before anything is written. After install, a verify step checks the merged result: managed blocks present, settings fragments merged, each registered hook pointing at an executable file.

**Basis.**

- Incident (in an existing tool): an installer that never overwrites will skip its safety rules and hook on any repository that already has an instructions file or settings, while its own health check still passes. Mature repositories are exactly the ones that have those files.

## 6. Enforcement lives on the server

**Decision.** In-session guards (hooks, permission rules) are treated as conveniences. The rung 3 checks require a continuous integration guard and branch protection on GitHub.

**Basis.**

- Reported: hooks fail open on a crash in most agents. Codex's documentation describes them as a guardrail and not a complete enforcement boundary.
- Judgment: a local script can be worked around by an agent that edits it, runs a different command, or is simply a different agent. The repository's protection settings cannot be changed from a pull request.

Protected paths prompt the person in an interactive session and are denied in unattended runs. Acceptance scenarios are blocked in both, because an agent that can edit the tests it is judged on eventually will. The in-session block covers edit tools and shell commands that write to that folder; a shell command can be disguised, so the CI guard is what finally rejects a change there.

## 7. Untrusted text is data

**Decision.** In the automation rungs, the job that reads issue or pull request text has no write permission. A separate job with no model credential validates the agent's structured output against a schema and an allowlist, then writes at most one comment and a capped set of labels. The review loop runs on the base branch's checkout, so instructions files and prompts come from trusted code and never from the pull request.

**Basis.**

- Incident: published research in 2025 and 2026 showed injected pull request titles and issue text causing several vendors' agents to leak tokens in continuous integration. Blocklists of commands did not hold; tool allowlists and narrow secret scoping did.
- Incident: a pull request that added text to an instructions file telling automated reviewers to ignore a credential upload (reported on X in November 2026; not independently confirmed).
- Reported: the lane split follows GitHub's agentic workflows "safe outputs" pattern ([reference](https://github.github.com/gh-aw/reference/safe-outputs)) and the base-checkout review design in [warpdotdev-demos/cloud-factory-demo](https://github.com/warpdotdev-demos/cloud-factory-demo).
- Incident (in an existing tool): a triage workflow that applies whatever label the agent returns, with no allowlist.

## 8. Limits on every loop

**Decision.** Every agent job has a time limit, a turn limit and a tool allowlist. One repository variable switches all agent workflows off. The implement loop stops claiming work when open agent pull requests reach a cap. The agent is told to stop after a fixed number of failed fix attempts; the turn and time limits are the hard caps.

**Basis.**

- Incident: a reported runaway agent spent about $1,400 in an hour on a tagging task.
- Reported: review capacity, not generation, is the bottleneck named by nearly every team that has written about this.

## 9. Improvement is proposed, never applied automatically

**Decision.** The tune mode turns repeated corrections into proposed checks, regression cases or rules. A correction counts only when it comes from a listed maintainer and has occurred at least twice. Changes go through a pull request.

**Basis.**

- Incident (in an existing tool): a daily loop that rewrites a review skill from any commenter's feedback, which lets an outsider steer the reviewer.
- Reported: the one production example we found of rules learned from corrections promotes them gradually and disables them on negative signal ([Cursor Bugbot](https://cursor.com/blog/bugbot-learning)).
- Reported: engineers who run such loops say the loop tends to rewrite instructions to please its judge.
- Judgment: check first, regression case second, written rule last. A check cannot be ignored and costs no context.

## 10. Agent-neutral core, thin adapters

**Decision.** Checks, guards and workflows are scripts and continuous integration. What differs per agent (instruction wiring, hook registration, permission files) is a small adapter selected by the repository's config.

**Basis.**

- Reported: `AGENTS.md` is read natively by most current agents, and `.agents/skills/` is a shared skills location for many of them.
- Judgment: hook formats differ between agents in event names, input shape and how a block is signalled. Keeping the logic in one script per guard and the format handling in one function per agent keeps an adapter small.

## Known limits of the guards

These are deliberate boundaries, stated so nobody relies on more than is there.

- **The in-session shell guard is best effort.** It judges the command line an agent types: redirect targets, arguments, inline code that names a protected path. It does not see inside a script file the agent wrote, an alias, or a path built at run time. It is there to stop the honest mistake ("make the gate pass"), and the CI guard on the pull request is what rejects the change.
- **Guard scripts are compared with the shipped version.** The audit and `verify` fail when a script under `.harness/bin` or `.harness/hooks` differs from the one this skill ships, unless its path is listed under `customized_scripts` in the config. After upgrading the skill, re-run the installer; until then those checks report the difference.
- **The workflow linter matches names and shapes.** It catches the known ways to break the two-lane split. A model key stored under an unrecognized secret name, a remote action that receives a secret, or a test-runner wildcard abused through its flags are not caught.
- **In the implement job, one setting stands between agent-written test code and the model key:** Claude Code's subprocess environment scrub. Use a dedicated, spend-capped key for that workflow.
- **The gates result shown on an agent pull request is a hint.** Gates run agent-written code, which can fake its own result. The path filter and diff guard run without executing the change; required CI on the pull request is the authority.
- **Nothing here has been run on GitHub Actions by the authors of version 0.1.** The workflows were linted, parsed and reviewed, and their shell steps were exercised locally.

## What this project does not claim

- That a factory is worth it for your team. Reported automation rates range from about a quarter of pull requests on a large product to most changes on a simple site, and they come from vendors.
- That an automated reviewer pays for itself. One vendor measured a 140% cost increase for a 7.5% quality gain; an independent reproduction found a 52% cost increase and no gain. The review loop is optional for that reason.
- That passing checks means the code is understood. Tests cover only the behavior someone thought to specify.

## Credits

Ideas were drawn from these projects and write-ups. Code was written fresh unless a file says otherwise.

- [addyosmani/factory](https://github.com/addyosmani/factory) (MIT): fail-closed gates, test proof, claiming work by branch push.
- [warpdotdev-demos/cloud-factory-demo](https://github.com/warpdotdev-demos/cloud-factory-demo) (MIT): trusted-base review, the triage label states.
- [disler/super-simple-software-factory](https://github.com/disler/super-simple-software-factory) (MIT): loop limits in code, checking writes after each agent step.
- [agent-readiness](https://github.com/harrydaihaolin/agent-readiness) (MIT): mechanical checks, the distinction between "not measured" and "failed"; some detection patterns are credited in the source.
- netresearch/agent-harness-skill: mechanical level model, parity between local hooks and continuous integration.
- Adobe's AI repo harness guide: checking instruction files for stale claims and size.
- OpenAI, "Harness engineering"; Anthropic's engineering posts on harnesses and evals; StrongDM's software factory notes.
