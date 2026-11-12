# Test Conventions

Language- and framework-agnostic. Baseline test rules for every project. Project-specific scenarios go in each project's `docs/TEST.md`.

## Core Principles

Tests verify "the expected behavior happens", not "the code is callable".

A passing test must insure against regression. If breaking one production line doesn't fail at least one test, that line is uncovered.

"Hundreds passing" is meaningless. One meaningful test beats a hundred empty ones.

## Root Causes (Why This Happens)

The Ban List below lists symptoms. To avoid repeating the same mistake in another form, look at the motive.

**C1. Mistaking test count for progress**
Filling blank slots to report "N tests added". → One meaningful > 100 empty.

**C2. Writing tests before understanding the spec**
Without knowing what the function should do, "at least it didn't crash" is the only available assertion. Write the spec in one sentence first, then the test.

**C3. Happy-path only**
If only success cases exist, "it succeeded" is the only thing left to assert. Symptom of not thinking about boundaries, error paths, or invariants.

**C4. Level confusion on smoke tests**
"Boots up / imports successfully" is legitimate at deploy/integration level, worthless as a unit test.

**C5. Defensive padding**
Adding bulk so the suite "looks thorough". Coverage numbers rise; regression detection doesn't.

**C6. Tautological assertions**
Comparing back the constant the constructor/factory just set. Production and test become equivalent — when the spec changes, both change together, so regression is never caught. (Upstream cause of B2, B6.)

Self-check: if you can't write one sentence naming the concrete regression this test catches, you're in one of C1–C6.

## Ban List

### B1. Reflection-only method existence check

The method body could be `throw new NotImplementedException()` and the test still passes. Metadata verification is not a test.

```csharp
// BAD
[Fact]
public void SetOwner_MethodExists()
{
    var method = typeof(DialogHelper).GetMethod("SetOwner");
    Assert.NotNull(method);
}

// GOOD
[Fact]
public void SetOwner_AppliesOwnerToWindow()
{
    var owner = new Window();
    var dialog = new Window();
    DialogHelper.SetOwner(dialog, owner);
    Assert.Same(owner, dialog.Owner);
}
```

### B2. Tautological assert (circular logic)

A test that mirrors back constants the factory/constructor set. Production and test are equivalent — they will change together, catching nothing.

```csharp
// BAD — production: Info() => new(Icon: "✓"). Test copies production verbatim.
[Fact]
public void Icon_InfoLevel_IsCheckmark()
{
    var vm = LogEntryViewModel.Info("ok");
    Assert.Equal("✓", vm.Icon);
}

// GOOD — verify what the user actually sees (rendered output, serialized result).
[Fact]
public void Info_Rendered_StartsWithSuccessGlyph()
{
    var rendered = LogRenderer.Render(LogEntryViewModel.Info("ok"));
    Assert.StartsWith("✓ ok", rendered);
}
```

### B3. `NotNull` as the only assertion

"An object was created" does not verify behavior. Assert at least one state or effect.

```python
# BAD
def test_loader():
    loader = Loader()
    assert loader is not None

# GOOD
def test_loader_fetches_profile():
    loader = Loader(fake_session)
    profile = loader.fetch("user1")
    assert profile.username == "user1"
    assert len(profile.posts) == 3
```

### B4. Mocking the SUT (Subject Under Test)

Dependencies may be mocked, but the thing being tested must not be. Mocking the SUT means the test only verifies the mock's behavior.

### B5. No-assert test / no-throw alone

`DoesNotThrow` alone is only acceptable in smoke tests. Normal tests assert a result.

### B6. 1:1 constant-mapping restatement

When production holds a `dict`/`switch` mapping keys to values and the test copies that mapping to compare — regression detection is zero. Verify how the mapping is *used*, not the mapping itself.

### B7. Mock call verification without argument/count check

`mock.assert_called()` alone is nearly worthless. Assert call arguments and count together.

```python
# BAD
def test_p4_edit_called():
    service.process(file)
    p4_mock.edit.assert_called()

# GOOD
def test_p4_edit_invoked_with_correct_path():
    service.process("C:/work/file.xlsx")
    p4_mock.edit.assert_called_once_with("C:/work/file.xlsx")
```

## Required Patterns

### R1. Mutation self-check

When adding a new test, deliberately break the production line it is meant to protect and confirm the test fails. If it doesn't fail, the test is meaningless — rewrite it.

One manual verification at write-time is sufficient (automation is separate).

### R2. Assert on observable results

- **User-visible output**: file content, HTTP response body, text shown in UI, log messages
- **External side effects**: external process call arguments (`p4 edit <path>`), saved DB rows, HTTP requests sent
- **Meaning of return value**: the interpreted result, not just the raw type

Do not assert internal field mappings or implementation details.

### R3. Mock only at system boundaries

Allowed:
- File system (a real temp directory is usually better)
- HTTP / external APIs
- DB (in-memory or a real container is better when feasible)
- External processes (`p4`, `excel`, `git`)

Forbidden:
- Domain objects within the same project
- Pure functions
- The SUT

### R4. Clear Arrange-Act-Assert separation

Arrange with realistic inputs close to real user data (actual sample files, real URL shapes).

### R5. Integration first, Unit supplements

E2E/Integration covers a complete user scenario. Unit tests harden specific branches, boundaries, and error cases within that scenario. A suite full of unit tests but no integration means "all parts pass, but the assembly might not."

### R6. Fixtures from captured payloads, not from memory

A hand-written fixture can satisfy a gate the real payload would fail, so a green suite proves the gate is *reachable*, not that live data reaches it. Code and fixture come from the same mental model; live payloads are the only independent source of shape.

For any gate that compares payload fields (owner, id, type, shape), write at least one fixture from a **captured** real payload. Treat "the live smoke passed" as a gate separate from "tests pass" — never let a green suite stand in for it. When a live smoke fails, the fixture that missed it is itself a finding: fix the fixture, not just the code.

## Test Types and Recommended Ratios

| Type | What It Verifies | Recommended % |
|---|---|---|
| Smoke | Entry point starts without crashing | <5% |
| Unit | Pure functions, branches, boundary values | 30–40% |
| Integration | Scenarios involving real dependencies (DB/FS/HTTP) | 40–50% |
| E2E | User workflow start-to-finish | 10–20% |

## Per-PR Checklist

When adding or modifying tests:

- [ ] Is there a one-line description of the concrete regression this test catches? (in the test name or a comment)
- [ ] Does breaking one production line cause this test to fail? (R1 manual check)
- [ ] Do assertions verify observable results or external side effects? (R2)
- [ ] Is the SUT un-mocked? (B4)
- [ ] No tautological constant comparisons? (B2, B6)
- [ ] No reflection-only existence checks? (B1)
- [ ] Do mock checks verify arguments and call counts? (B7)

## Project-Specific Application

Each project defines its core user scenarios and test gaps in `docs/TEST.md`. This document only defines *how* to write tests.
