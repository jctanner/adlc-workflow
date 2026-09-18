# adlc-workflow development notes

The core owns lifecycle order, legal transitions, task acceptance, plugin
activation, publication planning, effect recovery, and terminal status.

The `/adlc-workflow:adlc-workflow` skill services core tasks. It must not become a second
workflow interpreter or grant workers authority over canonical state.

Keep production, local, and eval modes on the same core contracts. This file is
guidance for a future implementation; it is not an implementation.
