# Python Coding Conventions

Based on PEP 8 and Google Python Style Guide.

## Naming
- `snake_case`: functions, variables, modules
- `PascalCase`: classes
- `UPPER_SNAKE_CASE`: constants
- `_leading_underscore`: internal/private

## Formatting
- 4 spaces indentation
- Max line length: 120
- Use trailing commas in multi-line collections

## Imports
- Group order: stdlib → third-party → local
- One import per line, no wildcard imports

## Type Hints
- Use type hints for function signatures
- Use `from __future__ import annotations` for forward refs

## Error Handling
- Catch specific exceptions, never bare `except:`
- Use context managers (`with`) for resource management

## Docstrings
- Google style docstrings for public APIs

## References
- https://peps.python.org/pep-0008/
- https://google.github.io/styleguide/pyguide.html
