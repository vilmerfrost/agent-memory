## Result

The HTTP client now retries a failed request 4 times instead of 2.

## Important changes

- `config.py` sets the new default retry count.
- A new unit test checks that the client uses 4 retries.

## Verification

All 212 unit tests pass. Nothing else was tested.

## Next action

None. You can review the diff when you have time.

## Skills, Plugins or MCPs used

None
