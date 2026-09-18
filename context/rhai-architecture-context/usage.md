# RHAI architecture context usage

Use the pinned RHOAI architecture context to ground feature refinement and
architecture review in existing platform components, APIs, dependencies, and
integration patterns. Active overlays take precedence over generated component
documentation when they conflict.

The profile follows `main`; each run must resolve and record the resulting
commit digest in its capture and bundle metadata so the context used by a run
remains reproducible.
