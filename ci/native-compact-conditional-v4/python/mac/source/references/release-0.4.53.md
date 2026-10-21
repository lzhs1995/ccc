# 0.4.53: Claude 2.1.303 reference rendering

Some 180-column Claude inputs pad a BODY row by 12 or 13 cells. The previous
matcher rejected the preserved notice and stopped before sending its key.
The matcher now recognizes the two measured four-row ASCII geometries using
the entire original marker, BODY path, SHA, byte count and receipt notice.
Canonical unpadded inputs remain valid. Raw input-row stability, complete
borders, at most one trailing cursor cell and actual dialog/duplicate-queue
checks remain in force. Ordinary work and Goal activity permit communication.

Existing pasted notices retain their original controller, body and paste fence.
Reconcile first. A native queue record means no further key or paste is allowed;
it is distinct from a direct native user record and full-body acceptance.
This release does not automatically submit historical stranded drafts.

All 310 immutable 0.4.52 members were statically verified against commit
`d2fd11d1005fdefc5770ebe14befd7313836b614` and manifest
`7fd8aae48af521d103525807c27e066fa0c761c40b430e2682d2b604d08b4fee`.
Its original 19-controller issuer scope and reservation capability are retained;
older issuers gain no reverse or future authority. Working registrations do
not need another stop/start simply because the display matcher changed.

Continue per-process upgrade verification from the 0.4.52 contract. Installation
alone does not prove automatic hooks, body reading or mailbox consumption.

The additional 134-column STATUS fixture has a 130-cell first row, a
93-cell continuation plus 18 padding cells, then a 128-cell row. Only this
measured geometry is admitted, with the entire original text still matched.
