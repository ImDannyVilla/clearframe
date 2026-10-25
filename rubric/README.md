# rubric

The creative director's policy (Danny, #4): `rubric.yaml` with the enabled checks, the flicker threshold, the fix library and `approvers`, plus the Slack card wording.

| File | What |
| --- | --- |
| `rubric.yaml` | The policy and the card's words. Comments in the file explain each setting. |
| `rubric.py` | Reads and checks `rubric.yaml`, standard library only. `python3 rubric/rubric.py` checks the file by hand. |

## Reading it

The GB10's offline paths have no PyYAML, so read the rubric through `rubric.py` rather than a YAML library:

```python
from rubric import rubric  # with the repo root on sys.path

policy = rubric.load()                       # refuses a rubric that breaks the contract
policy["checks"]                             # ["exposure_flicker", "road_markings"]
policy["flicker_threshold"], policy["flicker_return_frames"]
rubric.fix_lines(policy, "road_markings")    # ["Make sure: lane arrows point ..."]
```

`rubric.py` reads only a small part of YAML (its docstring lists it) and fails with the line number on anything else. `tests/test_rubric.py` checks that PyYAML, when installed, reads the file the same way.

## Rules

- **The checks come from what Blake's studio rejects takes for.** `road_markings` is the default noted check. The second is the flaw that shows up most in Blake's bad takes, and is added once they're logged in `answer_key.csv` (#6). `exposure_flicker` runs on every take in the background, with no flicker added to any take. `extra_finger` is the last fallback.
- **A new issue type goes into the contract first**, by pull request, with its card label in the issue-types table. Then add it to `ISSUE_TYPES` in `rubric.py`, to `card.category_labels`, and to the fix library. `tests/test_rubric.py` fails if the contract and `rubric.py` disagree.
- **Rubric order** is the order of `fix_library`. prompt-fix appends fixes in it, and the card counts follow it. `checks` must keep the same order.
- **One fix-library entry per enabled check**, written from the difference between Blake's bad and good prompts. A fallback may keep an entry ready; `rubric.py` refuses an enabled check without one.
- **The fix library is what the fixed takes were made from.** Change a line and the fixed takes must be regenerated from the new prompt. The test rebuilds 4-2's version 2 and checks its hash against the contract.
- **Card templates use only the contract's card fields.** `rubric.py` refuses any other placeholder, and the test renders the contract's two sample cards from the templates.
- The flicker threshold and `flicker_return_frames` are a first setting; check them against the worker's brightness series (#5).
