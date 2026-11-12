# Code Navigation Conventions

Two user-scope MCP servers answer code questions: `serena` (symbol-level, language-server backed) and `codebase-memory-mcp` (whole-repo graph). They are used together.

## Which tool for which question

| Question | Tool |
|---|---|
| Where is this symbol defined, who references it, what implements it | Serena: `find_symbol`, `find_referencing_symbols`, `find_declaration`, `find_implementations` |
| What is in this unfamiliar file | Serena: `get_symbols_overview`, instead of reading it whole |
| Call chain between two symbols, multi-hop relationships | codebase-memory-mcp: `trace_path`, `query_graph` |
| Architecture overview, change impact | codebase-memory-mcp: `get_architecture`, `detect_changes` |
| Dead code, semantic search across the repo | codebase-memory-mcp: `search_graph` (`max_degree: 0` with `exclude_entry_points`; `semantic_query`) |
| Strings, comments, config keys, non-code text | grep/glob |

- Serena's answer is exact for the symbol asked; the graph is the faster route across many files. A graph hit that drives an edit is confirmed with Serena or the real file first.

## Where they are available
- Both: main session, background `Agent` subagents, codex.
- Custom agent types get only what their `tools:` list names — `explore`, `verify-claims` and `adversarial-review` carry Serena lookups and graph queries; `synthesize-reviews` carries neither — hand it the results instead.
- Background `Agent` subagents do not get the built-in `LSP` tool; Serena covers those lookups.
- Delegation packets doing code lookup name the tool and the absolute project path.

## Serena

### Activate the project first
- Activation is per Serena process: one `activate_project` covers that session and every subagent sharing it; a separate process (codex) activates its own.
- Nothing auto-activates when the process's cwd is not a project root — `--project-from-cwd` looks for `.serena/project.yml` or `.git`.
- Symptom when skipped: `No active project. Ask the user to provide the project path`.

### Paths and line numbers
- `relative_path` is relative to the activated project root.
- `body_location` line numbers are 0-based — add 1 before quoting a `file:line` to a human.

### Edits go through Claude Code
- Serena's write tools (`replace_symbol_body`, `rename_symbol`, `insert_*`, `replace_in_files`) edit files from inside the Serena process, so the harness's own edit path — read-before-write tracking and the Edit/Write hooks — never sees them.
- Use `Edit`/`Write`; reach for a Serena write tool only when explicitly asked.

### Language server per language
- Python → pyright.
- C/C++ → clangd, which needs `compile_commands.json` in the project root.
- C# → Serena's bundled C# language server.

### Repo hygiene
- Activation writes an untracked `.serena/` into the project root; it is gitignored and never committed.

## codebase-memory-mcp

- Read-only: it never edits files.
- `index_repository` once per repo, from the main session, before any graph query is delegated; agent types carry query tools only. `index_status` / `list_projects` show what is indexed. Indexes auto-refresh after that.
- Every query takes `project`; `list_projects` prints the indexed names.
- A file the index does not cover looks the same as a file with no matches: run `check_index_coverage` on the paths an answer cites, and fall back to `search_code` or grep for a gap. A clean coverage result is not proof of completeness.
- Results paginate; read on before concluding something is absent.
- `get_code_snippet` returns source for a graph hit without opening the whole file.

## References
- https://oraios.github.io/serena/
- https://github.com/oraios/serena
