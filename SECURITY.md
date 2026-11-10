# Security and private data / 安全與私人資料

Do not put credentials or real session transcripts in public issues. If you find
a leaked credential, revoke or rotate it at its provider before discussing a
redacted reproduction. Removing a line from `main` does not remove Git history,
forks, caches or existing release archives.

Use GitHub's **Security → Report a vulnerability** when that private reporting
option is available. Otherwise open a public issue requesting a private contact
channel, without exploit details, tokens, personal paths or private records.

CCC configuration, profiles, local runtime state and model conversations belong
on the operator's machine. Public examples must contain placeholders or clearly
synthetic fixtures. `scripts/check_public_content.py` checks common accidental
disclosures in the current tracked tree; it does not inspect provider accounts,
prove all secrets absent, or certify the entire Git history. Four existing fake
credential sentinels are exempted by exact file path and value hash in
`scripts/public_content_fixtures.json`; there are no directory-wide secret
exemptions. Changes to that list require review.

繁體中文：請勿將憑證、真實對話或私人路徑貼到公開議題。發現憑證外洩時先撤銷
或輪替，再透過私人通報管道處理。刪除主分支的一行不代表歷史紀錄已清除。
自動掃描只涵蓋明列的模式與目前版本，不能取代人工審查。
