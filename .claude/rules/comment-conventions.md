# Comment Conventions

Based on Clean Code Ch.4 (Robert C. Martin) and Google's language style guides. Read this before adding any code comment or docstring.

## Core Principle

Explain yourself in code first. Comments are a last resort, not a substitute for clear naming, small functions, or honest structure.

Comments answer **WHY**, not **WHAT**. Well-named identifiers and short functions already say what the code does; comments earn their keep only when intent, constraint, or consequence isn't visible in the code itself.

Code comments and docstrings are written in **English** — see B6. UI-facing user strings (error messages, button labels) are separate and may use the product's locale.

## Default: No Comment

If removing the comment wouldn't confuse a future reader, don't write it.

Before adding a comment, try in order:
1. Rename the variable or function.
2. Extract the expression into a well-named local.
3. Split a long function into smaller named pieces.

If none of those resolve the confusion, then write the comment.

## When to Comment (Good Comments)

### G1. Intent / hidden constraint

Why this branch exists, why this order matters, why this magic value. Things that won't be obvious from reading the code six months later.

```python
# Sort by descending score; ties broken by recency so newer entries win.
items.sort(key=lambda x: (-x.score, -x.created_at))
```

### G2. Warning of consequences

Irreversible operations, race windows, performance cliffs, surprising side effects.

```csharp
// Caller must hold the write lock. This method does NOT acquire it
// because callers typically batch other writes inside the same lock.
```

### G3. Workaround for a specific bug

Link to the upstream **issue or bug ID** (durable), not your sprint/task ticket (rots after the sprint closes). The link must give a future reader something to investigate years later.

```js
// Workaround: Chrome 121 fires `change` before `input` on date pickers.
// https://bugs.chromium.org/p/chromium/issues/detail?id=1234567
```

### G4. Non-obvious algorithm clarification

When the algorithm itself is the surprise — bit tricks, numerical stability, off-by-one corners. A one-line note saves the next reader from re-deriving it.

### G5. Public API docstrings

At boundaries (libraries, public methods, exported functions), a docstring documents the contract: parameters, return values, exceptions, side effects, invariants, and call restrictions. The reader should not need to open the body to call the function.

Language-specific formats live in the References below.

## Ban List (Bad Comments)

### B1. Redundant — restating what the code shows

Step narration (`# Step 1: validate`, `# First... Finally...`) and a contract line that only renames its parameter (`@param price The price.`) are the same restatement. Where the docstring format requires the entry (G5, Google-style Args), the line states what the name does not carry: unit, range, default, nullability.

```python
# BAD
i += 1  # increment i

# BAD — docstring mirrors the function name
def get_user_by_id(user_id):
    """Get a user by ID."""
    ...
```

### B2. Noise — closing-brace labels, ASCII banners, section dividers

Use file structure and language features, not visual decoration. A plain category label (`# Main logic`, `# Helper functions`) or a decorative emoji (`# ✅ Validation`) is the same noise. Exception: a namespace-closing comment (`}  // namespace foo`) where the project's C++ style guide requires one.

```cpp
// BAD
} // end of class Bar

// BAD
// ============================
// === HELPER FUNCTIONS     ===
// ============================
```

### B3. Commented-out code

Delete it. Version control remembers; commented-out code rots and leaves readers wondering whether it should still run.

```python
# BAD
def handle(req):
    validate(req)
    # old_path:
    # result = legacy_process(req)
    # log.write(result)
    return new_process(req)
```

### B4. Journal / attribution / task reference

Who added it, when, which ticket, which sprint — this belongs in the commit message and PR description, not in source. It rots immediately and clouds `git blame` output.

License and copyright headers are legal notices, not attribution; keep them as the project's license requires.

```python
# BAD
# Added by jdoe on 2024-03-15 for JIRA-1234 (sprint 47 hotfix)
# Modified by asmith on 2024-04-02 to handle null case (PR #5678)
def normalize(s):
    ...
```

### B5. Compensation — explaining bad code instead of fixing it

If a function needs a paragraph of preamble to be understood, the function is too complex. Refactor instead of narrating.

```python
# BAD — comment props up an unclear name
def proc(x):
    # x is the user record; this returns whether the user can checkout,
    # which is true only when subscription is active AND credit > 0.
    return x[2] and x[5] > 0

# GOOD — name and structure carry the meaning
def can_checkout(user):
    return user.subscription_active and user.credit > 0
```

### B6. Wrong-language comments

Comments and docstrings are **English-only**, even when the project's chat or product copy is in another language. Mixed-language source defeats grep, breaks tooling assumptions, and confuses contributors who don't share the locale.

```python
# BAD
def 사용자_정규화(s):
    # 입력에서 공백을 제거하고 소문자로 변환
    return s.strip().lower()

# GOOD
def normalize_user(s):
    # Normalize once so later comparisons stay case-insensitive.
    return s.strip().lower()
```

### B7. Padding — a paragraph around a one-line fact

A WHY comment runs as many lines as it has facts. Cut a "because X, so Y, therefore Z" chain that restates one fact; keep each cause the constraint depends on. G3's bug link and G5's contract are facts, not padding.

## TODO Format

Use this shape for any deferred-work marker:

```
TODO: <link> - <description>
```

- `<link>` is an issue-tracker URL, PR URL, or short bug ID. It must lead a future reader somewhere durable.
- `<description>` is one short sentence stating what's missing.
- Do **not** attribute by username. `TODO(jdoe):` rots the moment the person changes teams.

```python
# GOOD
# TODO: https://github.com/org/repo/issues/42 - Handle UTF-16 surrogate pairs in input.

# BAD
# TODO(jdoe): fix this later
# TODO: refactor
```

`FIXME` and `HACK` follow the same `<marker>: <link> - <description>` shape. Avoid invented markers (`XXX`, `NOTE`) — three is enough.

## Per-PR Checklist

- [ ] Every surviving comment answers WHY, not WHAT.
- [ ] No commented-out code.
- [ ] No journal entries, attribution lines, or stale task references; durable issue/PR/bug links are allowed only inside TODO/FIXME/HACK markers or G3 workaround comments.
- [ ] All comments and docstrings are in English.
- [ ] TODO/FIXME/HACK markers carry a link and a one-line description.
- [ ] No noise comments (closing-brace labels, banners, section dividers); a namespace-closing comment the style guide requires is not noise.
- [ ] Public API has contract docs; private helpers skip docstrings unless required by language/tooling or by a non-obvious constraint worth recording.

## References

- Robert C. Martin, *Clean Code*, Chapter 4 ("Comments")
- https://google.github.io/eng-practices/review/reviewer/comments.html
- https://google.github.io/styleguide/pyguide.html#312-comments-and-docstrings (TODO format authority)
- https://google.github.io/styleguide/jsguide.html#jsdoc (Google JS docstring guidance)
- https://peps.python.org/pep-0257/ (PEP 257, Python docstrings)
- https://jsdoc.app/ (JSDoc format)
- https://tsdoc.org/ (TSDoc — TypeScript variant)
- https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/xmldoc/ (C# XML doc comments)
- https://www.doxygen.nl/manual/docblocks.html (C++ Doxygen)
