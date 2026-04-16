# AGENTS.md

Rules for any coding agent (AI or human) working in this repo.

---

## Pre-Commit Checklist

- [ ] Run `tofu fmt -recursive` — formatting is not optional
- [ ] Run `tofu validate` — must pass before committing
- [ ] No `.tfvars` files in commits — use `.gitignore`
- [ ] No secret defaults in `variables.tf` — use `sensitive = true`, no `default`
- [ ] New dependencies added to `versions.tf`

---

## Guardrails

1. **Never commit `.tfvars` files.** They may contain secrets. The `.gitignore` blocks them by name pattern.
2. **Never commit hardcoded credentials.** Tokens, keys, and passwords go in AWS Secrets Manager or env vars.
3. **Never modify state backend config** (`backends.tf`) without understanding the locking implications.
4. **Never edit `terraform.tfstate` directly.** Use `tofu state` commands.
5. **Do not skip `tofu validate`.** A plan that passes validation is not a guarantee of correctness, but one that fails validation is definitely wrong.

---

## Pull Request Discipline

- One logical change per PR (e.g., "add DLM snapshot policy" not "miscellaneous improvements").
- Link PRs to issues in PLAN.md.
- Describe what changed and why in the commit message — the "what" is visible in the diff; the "why" is not.

---

## When Unsure

- Read [PLAN.md](./PLAN.md) before making structural decisions.
- Read [CLAUDE.md](./CLAUDE.md) for conventions and boundaries.
- Err on the side of asking — this repo is in active revival; the plan may be stale on a detail.
