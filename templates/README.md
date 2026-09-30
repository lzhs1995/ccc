# Configuration examples

`_template.example.json` contains empty local profile credentials.

The plist files illustrate the service structure. For operational plists, use
`scripts/render-launchagents.sh OUTPUT_DIRECTORY` after `ccc install`; it
validates and pins the installed immutable runtime, escapes paths with plistlib,
and does not load services. `RUNTIME_DIR` is the installed release directory,
never a source checkout. `LABEL_PREFIX` defaults to `com.<current user>`.

Watcher labels use `CCC_LABEL_PREFIX`; stack label selection uses
`CMUX_STACK_LABEL_PREFIX` and its individual component overrides. When changing
an installation's labels, configure the controller to address those same labels.
