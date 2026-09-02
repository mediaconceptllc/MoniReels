# `.shape/` — the API's real JSON, frozen

These files are the backend's actual responses with every value stripped out.
`npm run verify-shape` assigns them to the types in `src/lib/types.ts`, so a
field renamed or removed on the server is a **compile error in CI** rather
than `undefined` in front of a user.

They are committed on purpose. Generated into `.gitignore` they would exist on
whichever machine last ran the tests and nowhere else — which means the check
would never run in CI, where it is the only thing looking at both halves.

## Regenerating

Whenever an API response changes shape:

```bash
cd backend
DATABASE_URL=postgresql://…/monireels_test pytest tests/test_frontend_shape.py
git add ../frontend/.shape
```

CI compares these files against the committed ones byte for byte, so a stale
`.shape/` fails the build — which is the point: the contract moved and nobody
looked at the other side.

## Why there is nothing in them

Every scalar is replaced by a placeholder of the same type (`0`, `0.0`, `""`,
`false`), `null` is kept because being nullable is part of the contract, and a
handful of union values (`kind`, `role`, `state`, …) survive because the type
they check is the value.

Two consequences worth knowing:

* **No real data.** The fixtures come from a synthetic project built inside
  the test, and the generator refuses to write a file that still holds a value
  — including one hiding in a KEY, which is why anything typed
  `Record<string, …>` collapses to a single `*`.
* **They only move when the CONTRACT moves.** A fixture carrying real values
  would change with the data, and every unrelated run would turn CI red.
