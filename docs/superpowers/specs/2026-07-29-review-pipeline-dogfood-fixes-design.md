# Review Pipeline Dogfood Fixes Design

## Goal

Make specialist review publication useful on a small stacked PR and include
safe, line-anchorable defects that exercise finding discovery and publishing.

## Changes

1. The publisher reuses the sticky comment ID returned by its first write when
   adding the native-review link. It must not depend on an immediately
   consistent comment-list query.
2. Specialist candidate instructions require `affected_location` to be an
   exact changed repository path or `path:line`. Honest changed-file locations
   may publish as file-level threads when no defensible line is available;
   invalid or non-changed locations remain general verification requests.
3. The deterministic handoff describes changed behavior, actual reviewed
   focuses, prepared-note status, and up to three useful human-review emphasis
   areas. It does not infer GitHub thread resolution state, remains sparse, and
   never copies detailed claims or evidence.
4. An inert evaluation fixture contains exactly two realistic review defects.
   The committed branch does not describe their categories, expected claims, or
   target lines; the manual oracle remains only in ignored local evaluation
   records.

## Success Criteria

- One managed sticky comment is created and subsequently patched by ID.
- Valid changed-file verification requests become resolvable FILE or LINE
  threads; invalid locations remain safe general comments.
- A degraded deterministic handoff helps a human understand expected changes,
  AI coverage, note status, and useful recheck areas.
- The stacked PR exposes two discoverable, changed-line canaries.
