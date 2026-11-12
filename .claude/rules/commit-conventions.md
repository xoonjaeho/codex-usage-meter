# Commit Conventions

Based on Conventional Commits v1.0.0. Read this before composing any commit message in a git-tracked project.

## Format

```
<type>[optional scope][!]: <description>

[optional body]

[optional footer(s)]
```

- `<type>` and `<description>` are required. `:` and a single space separate them.
- `[optional scope]` is a noun in parentheses naming the affected area, e.g. `fix(parser):`.
- `!` before the colon marks a breaking change. May be combined with a `BREAKING CHANGE:` footer.

## Types

| Type | When to use | SemVer impact |
|---|---|---|
| `feat` | New feature for users | MINOR |
| `fix` | Bug fix for users | PATCH |
| `docs` | Documentation only | — |
| `style` | Formatting, no code change | — |
| `refactor` | Code restructuring, no behavior change | — |
| `perf` | Performance improvement | PATCH |
| `test` | Adding or correcting tests | — |
| `build` | Build system, dependencies | — |
| `ci` | CI configuration, scripts | — |
| `chore` | Maintenance tasks, no production code change | — |

A breaking change (`!` or `BREAKING CHANGE:` footer) bumps MAJOR regardless of type.

## Body

- Begins one blank line after the description.
- Free-form; may span multiple newline-separated paragraphs.
- Explains *why* the change was made, not *what* (the diff already shows what).
- Plain words: a cluster of filler (`in order to`, `it is important to note`) or hype (`seamless`, `significantly improves`) reads as generated; rewrite the cluster. Quotes, identifiers, log or repro text and a precise lone term stay. No closing line praising the change (`This makes the code more maintainable`); end on the last concrete fact.

## Footers

- One blank line after the body.
- Format: `Token: value` or `Token #value`.
- Token uses `-` in place of whitespace, e.g. `Reviewed-by`, `Refs`, `Closes`.
- `BREAKING CHANGE` is the only token allowed to contain a space; `BREAKING-CHANGE` is synonymous.
- Value may span lines until the next valid token is observed.

## Breaking Changes

Two indicators (either or both):

1. `!` before the `:` — `feat(api)!: drop support for Node 16`
2. Footer — `BREAKING CHANGE: Node 16 is no longer supported`

`BREAKING CHANGE` MUST be uppercase. Other tokens are case-insensitive.

## Examples

```
feat: allow provided config object to extend other configs
```

```
fix(parser): array parsing issue when multiple spaces were contained in string
```

```
feat!: send an email to the customer when a product is shipped
```

```
docs: correct spelling of CHANGELOG
```

```
refactor(auth)!: rename SessionStore.set to SessionStore.put

The previous name conflicted with the Map-style API used elsewhere.
Migration: replace .set(k, v) with .put(k, v).

BREAKING CHANGE: SessionStore.set has been removed.
Refs: #142
```

## Per-Commit Checklist

- [ ] Type matches the actual change (`feat` for a new feature, `fix` for a bug fix, `chore`/`docs` for non-functional).
- [ ] Description starts lowercase, no trailing period, under ~72 characters.
- [ ] Scope (if used) is a single noun, not a sentence.
- [ ] Breaking changes have `!` and/or a `BREAKING CHANGE:` footer.
- [ ] Body explains *why*, not *what*.
- [ ] No decorative emoji in the subject or body; quoted input or output and attribution lines the harness supplies stay as given.
- [ ] No squashed unrelated changes — one logical change per commit.

## References

- https://www.conventionalcommits.org/en/v1.0.0/
- https://semver.org/
