"""CURRENT's reviewed data interpreter; never load code from an old release."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

LABEL = 'org.multi-agent-collaboration.executor-reask-scheduler'
LEGACY_FILES = ('VERSION', 'scripts/executor_reask.py', 'scripts/executor_ready.py',
                'scripts/cmux_bridge.py', 'scripts/executor_reply.py',
                'scripts/executor_reask_lifecycle.py')
# New consumers pin the code that grants identity/consumption authority. Keep
# the reviewed historical file set intact: compatibility never upgrades pins.
FILES = LEGACY_FILES + ('scripts/executor_reask_identity.py',
                       'scripts/executor_reask_protocol.py',
                       'scripts/executor_reply_advisory.py',
                       'scripts/cmux_native_delivery.py',
                       'scripts/cmux_workspace_guard.py',
                       'scripts/cmux_daemon_identity.py',
                       'scripts/cmux_foreground_thread.py',
                       'scripts/cmux_coordination_paths.py',
                       'scripts/executor_proactive_hook.py', 'scripts/native_user_record.py')
SCHEDULER = 'scripts/executor_reask_scheduler.py'
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RELEASE_BYTES = 64 * 1024 * 1024
MAX_RELEASE_FILES = 2048
# Raw immutable manifests reviewed with LEGACY; never learn trust from a record
# or from a manifest's self-reported version/tree hash.
LEGACY_MANIFESTS = {
    '0.4.57-e8685485a9c5': '1a2cbd08d0b83a1afab765ce6635815d2644abd8d3c8577d542f41e5464ae85e',
    '0.4.56-bea19f7ff44e': '0fdb8da8a94dbe277a267430949cf911324b3220d4098ffdd1b0508d0057fc7a',
    '0.4.54-f86a9efcf10f': '5ba32999d417a9c97bbbcc8bfe4bddf9c347ca5a6526576c3c5b91e25d601f8b',
 '0.4.55-8064e2645b0e': '954513900d3c19d8bd8698d5741db865d1e005baa2d3ac65927e45199dc00e63',
    '0.4.53-568efc13c7ae': 'b28ca93e025a9e0fe01fc2da2d21d10661f08a7a4f3341e592378a38958bab16',
    '0.4.52-0ca4d8002da7': '7fd8aae48af521d103525807c27e066fa0c761c40b430e2682d2b604d08b4fee',
    '0.4.51-587295817cef': 'eaa374d774766d606753a748c40dd3460dd518be09c9a2fc7ce80928014445a3',
    "0.4.50-3dd2945b5069": "4c91053a01d9a69e497635452330f15ed06a3931d4b50886c1e42e3d36bee2cc",
    "0.4.49-689dda6455be": "67c72f782ec5b055fe534b20b7bc7ba876a5a4914685d2877a32693d3ea81279",
    '0.4.48-3b4304d24f90': '1044ff4ba71cc9cb7ea51fd48bc73db366b5850f2e384935b31399f87e756f5c',
    "0.4.47-46395e3b21e5": "23b2442f1fb3ee42388a1dbb0057a12df950754e4ed537d2776d4507cd514698",
    "0.4.46-37e945dea2e3": "a4cfc0ce983e7cd533bb21461bc7f4938f8e859c2bbd30c32d87678098521688",
    "0.4.44-2b9bce3b41eb": "8ac64d1b81441e44d485ba604d3027dcbf63f13fb4375fe64a7e975a00b7a171",
    '0.4.42-5947b2d1f8aa': 'a2696c0f4d62e69b8ea7af3552c60838ecbfb1eda1456a424897af43441a9cfd',
    '0.4.41-28b36490b149': '693cf37272b326392bfdb5506746ca64e6582c90a618785c8b91e20bd05a8222',
    '0.4.37-c7258c383218': 'c474f812aee100ec2f4af079a6f73661fc79994157f6e325d75861600ed242bb',
    '0.4.36-8ab59dbb5a5a': 'c23ae263336ceb525886c4b6d7797d09cc4bc12706e744f471db29efbebe89f9',
    '0.4.35-e62f35828c7a': '0084ec08de7195a4ba2b8d5bc22e6495b56da387113d9c1308e380c5d9abf53b',
    '0.4.28-ae66245771d0': '31579cfab68b908561a6b6833079c5942bc27e05fe097f6f3249b3cef81cac6e',
    '0.4.30-ff75100795a9': '80f925fb74964f2cc0d72d7464f418e23ab637d504f78c79e4dd47fb26fe815f',
    '0.4.31-79ae83e9d5da': 'bc369ef7c81e7a4c34c4001736df61a99b634350248a2299def68e92efa098f5',
    '0.4.32-c7338a400bf4': '682e2215a09381c2e565eb73b142f360ec506c4a428a776423f64ff22cdd77d5',
    '0.4.33-09de16ceb823': 'fd89bd0508862f30271c6201346f8f85472c47bfeaba45635f868c4d7d8ee08a',
    '0.4.34-d333557fb498': 'd4039ba7078eeaae7df91dcbd83ff886408539ff50874537ba0e555f95b8c917',
}
# These COMPLETE combinations were reviewed together. A version string or one
# matching file is not a compatibility grant.
LEGACY = {
    '0.4.57-e8685485a9c5': {'VERSION': '1059be38a796db83568ee914a4df98337e539a1f011cec58d512338402b8b391',
     'scripts/executor_reask.py': '8a70ba3529f889013b467374c28a367c3d93dcf0d3dfa6aa83179ef4a9b4bcaf',
     'scripts/executor_ready.py': '1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79',
     'scripts/cmux_bridge.py': '20cd5c34015b7abde4cad718e2dccf6a0ca44ba0a5e7e19406b589f0962acff4',
     'scripts/executor_reply.py': '160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312',
     'scripts/executor_reask_lifecycle.py': 'b35e6afac1884e84b2a6888b21331fc3d1daea8cdd47f75f7f7b20667772d813',
     'scripts/executor_reask_identity.py': 'c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2',
     'scripts/executor_reask_protocol.py': '8bd95ada165a5e054718e6757169d8bf1746e866feff21b5251e75508baa0be6',
     'scripts/executor_reply_advisory.py': 'e2c76da58cf5b970d37627d57e984940776976cc6529bb553a9d1f841be822bb',
     'scripts/cmux_native_delivery.py': '5478f625084e3046f2bde52cfb2d2092de3016b9c09446a0aaa3a0dd5fa493a4',
     'scripts/cmux_workspace_guard.py': '43f652737f458cf8b007672d58b817e58782321baef1f0ab97605b77925fc46f',
     'scripts/cmux_daemon_identity.py': '25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb',
     'scripts/cmux_foreground_thread.py': '892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3',
     'scripts/cmux_coordination_paths.py': 'f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32',
     'scripts/executor_proactive_hook.py': '3ddeeb36ca47889e831989d87793fdb6df207dbb11a8b4d3eb23c84aca893152',
     'scripts/native_user_record.py': '3270910e1ceeb9a817b9783953d59ebc71c54966a17a57705be1015c862548e2',
     'scripts/executor_reask_scheduler.py': 'bfdf1ec0d29515d5f5455045a4f728d3bb58d2c5d8ec8c03fc8388d8f3bb6524'},
    '0.4.56-bea19f7ff44e': {'VERSION': '85dabdb03e5ebab86362461de75b0df9ac024e6499a6d0f357b143ffd2b7a019',
                         'scripts/executor_reask.py': '8a70ba3529f889013b467374c28a367c3d93dcf0d3dfa6aa83179ef4a9b4bcaf',
                         'scripts/executor_ready.py': '1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79',
                         'scripts/cmux_bridge.py': '20cd5c34015b7abde4cad718e2dccf6a0ca44ba0a5e7e19406b589f0962acff4',
                         'scripts/executor_reply.py': '160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312',
                         'scripts/executor_reask_lifecycle.py': 'b35e6afac1884e84b2a6888b21331fc3d1daea8cdd47f75f7f7b20667772d813',
                         'scripts/executor_reask_identity.py': 'c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2',
                         'scripts/executor_reask_protocol.py': '61995bc3a07c8021afe86a43590ce84cb6f00e21bad3e4009c6f37ee209481a8',
                         'scripts/executor_reply_advisory.py': 'e2c76da58cf5b970d37627d57e984940776976cc6529bb553a9d1f841be822bb',
                         'scripts/cmux_native_delivery.py': '5478f625084e3046f2bde52cfb2d2092de3016b9c09446a0aaa3a0dd5fa493a4',
                         'scripts/cmux_workspace_guard.py': '43f652737f458cf8b007672d58b817e58782321baef1f0ab97605b77925fc46f',
                         'scripts/cmux_daemon_identity.py': '25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb',
                         'scripts/cmux_foreground_thread.py': '892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3',
                         'scripts/cmux_coordination_paths.py': 'f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32',
                         'scripts/executor_proactive_hook.py': '77e9cd2fbb47b7416fd74eac82966975100963e8d40af764d2d263f6833d130b',
                         'scripts/native_user_record.py': '3270910e1ceeb9a817b9783953d59ebc71c54966a17a57705be1015c862548e2',
                         'scripts/executor_reask_scheduler.py': 'bfdf1ec0d29515d5f5455045a4f728d3bb58d2c5d8ec8c03fc8388d8f3bb6524'},
    '0.4.54-f86a9efcf10f': {'VERSION': '6b100b495c461298b554555dfea9ac13368acba909261f7b2e6ce84c8ec693bd',
                         'scripts/executor_reask.py': '5273009e2c225081457c6594cc9fadfea077217376c5afc7ed2da694462c3bbb',
                         'scripts/executor_ready.py': '1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79',
                         'scripts/cmux_bridge.py': 'b2b9e39b62523502714191ed6c5e43a2897442a2a161932e144894e5212c1bee',
                         'scripts/executor_reply.py': '160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312',
                         'scripts/executor_reask_lifecycle.py': '9a93b642cf78f45eaaa713affaf0d5a9640faabc65970f474b1d5be11228fd09',
                         'scripts/executor_reask_identity.py': 'c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2',
                         'scripts/executor_reask_protocol.py': 'afc5ffb563aed7afde9740355d7ef6351c950ac66bb82e3ddece0882b18f1c3b',
                         'scripts/executor_reply_advisory.py': 'e2c76da58cf5b970d37627d57e984940776976cc6529bb553a9d1f841be822bb',
                         'scripts/cmux_native_delivery.py': 'e20465a6809ab195c2a2743b6b8789243b6d0430c82fddf9809108e7d7ef9698',
                         'scripts/cmux_workspace_guard.py': '43f652737f458cf8b007672d58b817e58782321baef1f0ab97605b77925fc46f',
                         'scripts/cmux_daemon_identity.py': '25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb',
                         'scripts/cmux_foreground_thread.py': '892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3',
                         'scripts/cmux_coordination_paths.py': 'f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32',
                         'scripts/executor_reask_scheduler.py': 'bfdf1ec0d29515d5f5455045a4f728d3bb58d2c5d8ec8c03fc8388d8f3bb6524'},
 '0.4.55-8064e2645b0e': {'VERSION': '07d6c12306b34e5e81c44a255d4f31f8ee96be4fbcccee2789cc4c03b6a2db0b',
                         'scripts/executor_reask.py': '5273009e2c225081457c6594cc9fadfea077217376c5afc7ed2da694462c3bbb',
                         'scripts/executor_ready.py': '1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79',
                         'scripts/cmux_bridge.py': '20cd5c34015b7abde4cad718e2dccf6a0ca44ba0a5e7e19406b589f0962acff4',
                         'scripts/executor_reply.py': '160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312',
                         'scripts/executor_reask_lifecycle.py': '9a93b642cf78f45eaaa713affaf0d5a9640faabc65970f474b1d5be11228fd09',
                         'scripts/executor_reask_identity.py': 'c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2',
                         'scripts/executor_reask_protocol.py': 'afc5ffb563aed7afde9740355d7ef6351c950ac66bb82e3ddece0882b18f1c3b',
                         'scripts/executor_reply_advisory.py': 'e2c76da58cf5b970d37627d57e984940776976cc6529bb553a9d1f841be822bb',
                         'scripts/cmux_native_delivery.py': 'e20465a6809ab195c2a2743b6b8789243b6d0430c82fddf9809108e7d7ef9698',
                         'scripts/cmux_workspace_guard.py': '43f652737f458cf8b007672d58b817e58782321baef1f0ab97605b77925fc46f',
                         'scripts/cmux_daemon_identity.py': '25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb',
                         'scripts/cmux_foreground_thread.py': '892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3',
                         'scripts/cmux_coordination_paths.py': 'f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32',
                         'scripts/executor_reask_scheduler.py': 'bfdf1ec0d29515d5f5455045a4f728d3bb58d2c5d8ec8c03fc8388d8f3bb6524'},
    '0.4.53-568efc13c7ae': {'VERSION': '738fe1d0848e080d87a857cf54d0ad017a0a7ed260ed3c3f5ab4e376319849cb',
                             'scripts/executor_reask.py': '5273009e2c225081457c6594cc9fadfea077217376c5afc7ed2da694462c3bbb',
                             'scripts/executor_ready.py': '1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79',
                             'scripts/cmux_bridge.py': '25c50be59ce55ec8911828648729beb6eacd49dad7b4601d942391227622f75c',
                             'scripts/executor_reply.py': '160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312',
                             'scripts/executor_reask_lifecycle.py': '9a93b642cf78f45eaaa713affaf0d5a9640faabc65970f474b1d5be11228fd09',
                             'scripts/executor_reask_identity.py': 'c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2',
                             'scripts/executor_reask_protocol.py': '220b97e778ca4bd247bf699272bfcd830e90dc08f88328f2adc07878236114d5',
                             'scripts/executor_reply_advisory.py': 'e2c76da58cf5b970d37627d57e984940776976cc6529bb553a9d1f841be822bb',
                             'scripts/cmux_native_delivery.py': 'bd2249ec9f383f187b53c2036042ccabf3e968c509d5e31599cf6b13592fb656',
                             'scripts/cmux_workspace_guard.py': '2fe39e7e1b545d7ac4a442c8e5f6d04bfd0343b506fbb6ae3feece182456e41b',
                             'scripts/cmux_daemon_identity.py': '25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb',
                             'scripts/cmux_foreground_thread.py': '892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3',
                             'scripts/cmux_coordination_paths.py': 'f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32',
                             'scripts/executor_reask_scheduler.py': 'bfdf1ec0d29515d5f5455045a4f728d3bb58d2c5d8ec8c03fc8388d8f3bb6524'},
    '0.4.52-0ca4d8002da7': {'VERSION': 'f69d7dffecc2ea5cde949a65edc36980a07c092dbace9d10a414e64287feb7a2',
                             'scripts/executor_reask.py': '5273009e2c225081457c6594cc9fadfea077217376c5afc7ed2da694462c3bbb',
                             'scripts/executor_ready.py': '1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79',
                             'scripts/cmux_bridge.py': '84db1c47afdc4506a40c8b3696a9ddf8aa91e15a03e36d14c34efefe6bf0573a',
                             'scripts/executor_reply.py': '160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312',
                             'scripts/executor_reask_lifecycle.py': '9a93b642cf78f45eaaa713affaf0d5a9640faabc65970f474b1d5be11228fd09',
                             'scripts/executor_reask_identity.py': 'c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2',
                             'scripts/executor_reask_protocol.py': 'e3eca6ce10934d12da3ad6dceb4c87224b5fa51acc4ed4d89d5bd7cb4c5c0ce6',
                             'scripts/executor_reply_advisory.py': 'e2c76da58cf5b970d37627d57e984940776976cc6529bb553a9d1f841be822bb',
                             'scripts/cmux_native_delivery.py': 'bd2249ec9f383f187b53c2036042ccabf3e968c509d5e31599cf6b13592fb656',
                             'scripts/cmux_workspace_guard.py': '2fe39e7e1b545d7ac4a442c8e5f6d04bfd0343b506fbb6ae3feece182456e41b',
                             'scripts/cmux_daemon_identity.py': '25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb',
                             'scripts/cmux_foreground_thread.py': '892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3',
                             'scripts/cmux_coordination_paths.py': 'f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32',
                             'scripts/executor_reask_scheduler.py': 'bfdf1ec0d29515d5f5455045a4f728d3bb58d2c5d8ec8c03fc8388d8f3bb6524'},
    '0.4.51-587295817cef': {'VERSION': '5e726fc4c62b3affd1ce4bdf0bcb18b3ab9c0ce0827cb52fa14c7283ccb0d1f9',
                             'scripts/executor_reask.py': '5273009e2c225081457c6594cc9fadfea077217376c5afc7ed2da694462c3bbb',
                             'scripts/executor_ready.py': '1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79',
                             'scripts/cmux_bridge.py': '84db1c47afdc4506a40c8b3696a9ddf8aa91e15a03e36d14c34efefe6bf0573a',
                             'scripts/executor_reply.py': '160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312',
                             'scripts/executor_reask_lifecycle.py': '9a93b642cf78f45eaaa713affaf0d5a9640faabc65970f474b1d5be11228fd09',
                             'scripts/executor_reask_identity.py': 'c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2',
                             'scripts/executor_reask_protocol.py': 'd3f944ffddf9ef645467d0c27c62585c7ff4bd499189bff37048c8fd93a0a34e',
                             'scripts/executor_reply_advisory.py': 'e2c76da58cf5b970d37627d57e984940776976cc6529bb553a9d1f841be822bb',
                             'scripts/cmux_native_delivery.py': '6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d',
                             'scripts/cmux_workspace_guard.py': '2fe39e7e1b545d7ac4a442c8e5f6d04bfd0343b506fbb6ae3feece182456e41b',
                             'scripts/cmux_daemon_identity.py': '25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb',
                             'scripts/cmux_foreground_thread.py': '892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3',
                             'scripts/cmux_coordination_paths.py': 'f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32',
                             'scripts/executor_reask_scheduler.py': 'bfdf1ec0d29515d5f5455045a4f728d3bb58d2c5d8ec8c03fc8388d8f3bb6524'},
    "0.4.50-3dd2945b5069": {
        "VERSION": "c8497efc4e941014caa383228c9bde9dc5c5553fa18e996c45be31e85877a418",
        "scripts/executor_reask.py": "5273009e2c225081457c6594cc9fadfea077217376c5afc7ed2da694462c3bbb",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/cmux_bridge.py": "84db1c47afdc4506a40c8b3696a9ddf8aa91e15a03e36d14c34efefe6bf0573a",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "9a93b642cf78f45eaaa713affaf0d5a9640faabc65970f474b1d5be11228fd09",
        "scripts/executor_reask_identity.py": "c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2",
        "scripts/executor_reask_protocol.py": "3ee225f141c560354481a5f7f0f08ecc39178fa2c71e757c68b06bc29a0aab1e",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "2fe39e7e1b545d7ac4a442c8e5f6d04bfd0343b506fbb6ae3feece182456e41b",
        "scripts/cmux_daemon_identity.py": "25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_coordination_paths.py": "f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32",
        "scripts/executor_reask_scheduler.py": "bfdf1ec0d29515d5f5455045a4f728d3bb58d2c5d8ec8c03fc8388d8f3bb6524"
    },
    "0.4.49-689dda6455be": {
        "VERSION": "f6d6cc4551bd55c7dfee4024da45b3190841faf4f17113ddf63cd6488881b7c9",
        "scripts/executor_reask.py": "5273009e2c225081457c6594cc9fadfea077217376c5afc7ed2da694462c3bbb",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/cmux_bridge.py": "84db1c47afdc4506a40c8b3696a9ddf8aa91e15a03e36d14c34efefe6bf0573a",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "9a93b642cf78f45eaaa713affaf0d5a9640faabc65970f474b1d5be11228fd09",
        "scripts/executor_reask_identity.py": "c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2",
        "scripts/executor_reask_protocol.py": "f0726c643e9b1716489596d8d967e0d0af8ba54a0e494e8bd5ff902ee36c4092",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "2fe39e7e1b545d7ac4a442c8e5f6d04bfd0343b506fbb6ae3feece182456e41b",
        "scripts/cmux_daemon_identity.py": "25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_coordination_paths.py": "f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32",
        "scripts/executor_reask_scheduler.py": "5e4859981962293443d4a61ad678e80ad4ec513a21da1d270e11fe502488ebdc",
    },
    '0.4.48-3b4304d24f90': {'VERSION': 'adf4ddde62ee2f5f7a3c3aa5476f54c7c0535b8db9b84eeb597e716fa2b65584',
                             'scripts/executor_reask.py': 'af526b7436c79349c5ae82c320eeddf5133e65c3ebebfe42e6e51e164fff70a8',
                             'scripts/executor_ready.py': '1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79',
                             'scripts/cmux_bridge.py': '84db1c47afdc4506a40c8b3696a9ddf8aa91e15a03e36d14c34efefe6bf0573a',
                             'scripts/executor_reply.py': '160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312',
                             'scripts/executor_reask_lifecycle.py': 'c9cd7f24e0a06b1112a3bff6bc16d20056ef89d02c026d90b909ea93c8175470',
                             'scripts/executor_reask_identity.py': 'c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2',
                             'scripts/executor_reask_protocol.py': 'a077ca9bb95c10ff7480aa9750522b3fb4ffea20de7f806688dc3337806e9849',
                             'scripts/executor_reply_advisory.py': 'e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a',
                             'scripts/cmux_native_delivery.py': '6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d',
                             'scripts/cmux_workspace_guard.py': '2fe39e7e1b545d7ac4a442c8e5f6d04bfd0343b506fbb6ae3feece182456e41b',
                             'scripts/cmux_daemon_identity.py': '25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb',
                             'scripts/cmux_foreground_thread.py': '892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3',
                             'scripts/cmux_coordination_paths.py': 'f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32',
                             'scripts/executor_reask_scheduler.py': '89050b03e2dca4e9e0f362fe0ce8f22456076dd59f845dd37b3e61261fa5451d'},
    "0.4.47-46395e3b21e5": {
        "VERSION": "404e2038a3e1410e4e9085ccccdbcb346e8930c1c2517df14adafe14a0a09af6",
        "scripts/executor_reask.py": "af526b7436c79349c5ae82c320eeddf5133e65c3ebebfe42e6e51e164fff70a8",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/cmux_bridge.py": "7a2458ef02627587ce8ebeadd5609ceac7517b79bcab9ce108cf71ca87a62de6",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "c9cd7f24e0a06b1112a3bff6bc16d20056ef89d02c026d90b909ea93c8175470",
        "scripts/executor_reask_identity.py": "c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2",
        "scripts/executor_reask_protocol.py": "24502165bd71640208b914fb0bacf59aa95f68e6f49034257a2176cd5bb5a8de",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "2fe39e7e1b545d7ac4a442c8e5f6d04bfd0343b506fbb6ae3feece182456e41b",
        "scripts/cmux_daemon_identity.py": "25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_coordination_paths.py": "f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32",
        "scripts/executor_reask_scheduler.py": "89050b03e2dca4e9e0f362fe0ce8f22456076dd59f845dd37b3e61261fa5451d",
    },
    "0.4.46-37e945dea2e3": {
        "VERSION": "51a607266bcd34905e4ad816b2a6a1c9c133c7843b5b7a0c897c3b0736da4afb",
        "scripts/executor_reask.py": "3bdca064f7459b274c6706d306987529f144824da6652189a0c9d0ef7b5cf289",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/cmux_bridge.py": "7a2458ef02627587ce8ebeadd5609ceac7517b79bcab9ce108cf71ca87a62de6",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "7800c1f675e482b363d39541b89d7c8a1438a69e2cade8d16f8aa3e9eab7a260",
        "scripts/executor_reask_identity.py": "c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2",
        "scripts/executor_reask_protocol.py": "161674c71668055a46914818deb1ce4a21d4e33751290229019c735d36894e1e",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "2fe39e7e1b545d7ac4a442c8e5f6d04bfd0343b506fbb6ae3feece182456e41b",
        "scripts/cmux_daemon_identity.py": "25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_coordination_paths.py": "f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32",
        "scripts/executor_reask_scheduler.py": "89050b03e2dca4e9e0f362fe0ce8f22456076dd59f845dd37b3e61261fa5451d",
    },
    "0.4.44-2b9bce3b41eb": {
        "VERSION": "28623c6e8738ecc960c8f5bdd10b6b7c5a06d4f700ff3823a4131258d3db848f",
        "scripts/cmux_bridge.py": "7a2458ef02627587ce8ebeadd5609ceac7517b79bcab9ce108cf71ca87a62de6",
        "scripts/cmux_coordination_paths.py": "f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32",
        "scripts/cmux_daemon_identity.py": "25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "93894510540c9b6eaa0518e57b09652c7dd08727edbdad57f04e1e4379d211c2",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/executor_reask.py": "3bdca064f7459b274c6706d306987529f144824da6652189a0c9d0ef7b5cf289",
        "scripts/executor_reask_identity.py": "c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2",
        "scripts/executor_reask_lifecycle.py": "7800c1f675e482b363d39541b89d7c8a1438a69e2cade8d16f8aa3e9eab7a260",
        "scripts/executor_reask_protocol.py": "32a0ddc916c8049c79a04203dd0a4a29b07bce7f9c9843190d5447940764897b",
        "scripts/executor_reask_scheduler.py": "89050b03e2dca4e9e0f362fe0ce8f22456076dd59f845dd37b3e61261fa5451d",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a",
    },
    "0.4.42-5947b2d1f8aa": {
        "VERSION": "8902ddb6e9cf51673ee72a5c5a282b88e1cd4fefca41257fd3ab6b8dfb3a68b5",
        "scripts/cmux_bridge.py": "7a2458ef02627587ce8ebeadd5609ceac7517b79bcab9ce108cf71ca87a62de6",
        "scripts/cmux_coordination_paths.py": "f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32",
        "scripts/cmux_daemon_identity.py": "25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "93894510540c9b6eaa0518e57b09652c7dd08727edbdad57f04e1e4379d211c2",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/executor_reask.py": "3bdca064f7459b274c6706d306987529f144824da6652189a0c9d0ef7b5cf289",
        "scripts/executor_reask_identity.py": "c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2",
        "scripts/executor_reask_lifecycle.py": "7800c1f675e482b363d39541b89d7c8a1438a69e2cade8d16f8aa3e9eab7a260",
        "scripts/executor_reask_protocol.py": "25b695fe8d0f3d28f1fcaf95755527cf7636ef07376e9a9961dda0064c442342",
        "scripts/executor_reask_scheduler.py": "89050b03e2dca4e9e0f362fe0ce8f22456076dd59f845dd37b3e61261fa5451d",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a",
    },
    "0.4.41-28b36490b149": {
        "VERSION": "cb3208f8d22502a2cbbe877c4be79cfdcddaafcb078385e2c0a8c3e292413aea",
        "scripts/cmux_bridge.py": "7a2458ef02627587ce8ebeadd5609ceac7517b79bcab9ce108cf71ca87a62de6",
        "scripts/cmux_coordination_paths.py": "f8399aec9644a95bb05bd8b74048782c3e14b70dc9333aee9ca901cea94e8f32",
        "scripts/cmux_daemon_identity.py": "25e84742779316635d0e840d92b0213c903f46a3c96e5877eae087462745fdfb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "93894510540c9b6eaa0518e57b09652c7dd08727edbdad57f04e1e4379d211c2",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/executor_reask.py": "234400b19f35a5d97b01d48efba368348410d9f376383037bc5cee1ff82817ee",
        "scripts/executor_reask_identity.py": "c9a8542bb91d872bab910498083f478b6fded4c0dd867a208bc105acad7696a2",
        "scripts/executor_reask_lifecycle.py": "e713c6e9d7483dd1f2a370123eba1f8bb803c286d8fb2803c85518d9e5b4c604",
        "scripts/executor_reask_protocol.py": "a50b9e469a8a0f5ea5f89c9b7e403989375ab1eeaf2cb17fc06d8216b54ab33a",
        "scripts/executor_reask_scheduler.py": "89050b03e2dca4e9e0f362fe0ce8f22456076dd59f845dd37b3e61261fa5451d",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a"
    },
    "0.4.37-c7258c383218": {
        "VERSION": "9f1347ce1ed3cc5c0f8c037c540b6eb6da327d4a5d97255e95f57789e2e7e82b",
        "scripts/cmux_bridge.py": "7a2458ef02627587ce8ebeadd5609ceac7517b79bcab9ce108cf71ca87a62de6",
        "scripts/cmux_daemon_identity.py": "886ef37091b1d6279c351c5ae2a62d2e3bf1980bb4c2524da406927918dd06eb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "93894510540c9b6eaa0518e57b09652c7dd08727edbdad57f04e1e4379d211c2",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/executor_reask.py": "e70984d564876efd48b3244a44449aa5fb27a04fcb6238a74b41251e6583e168",
        "scripts/executor_reask_identity.py": "2e68a94bd1c0acba6a5f292568cb8f039bc25932cecb2fc89e809b6f093df7b1",
        "scripts/executor_reask_lifecycle.py": "f7d3dc7b1713bd4dc8d6db8aca0990b3c8bdb186fd24d6ed5902d146a5c08426",
        "scripts/executor_reask_protocol.py": "5f22eca34c24cad4cfc87195411752eb9c06bb8fa514eab8cface319352775ce",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a",
        "scripts/executor_reask_scheduler.py": "e60771ea2092df69cb979fabbeb8009d085b7e2cc45d5c0c54cd2758a04cda8b"
    },
    "0.4.36-8ab59dbb5a5a": {
        "VERSION": "3fe5a208c7783976c775da2adb21f9c69db4ae5fbc773a06955c0fa7ac675cef",
        "scripts/cmux_bridge.py": "7a2458ef02627587ce8ebeadd5609ceac7517b79bcab9ce108cf71ca87a62de6",
        "scripts/cmux_daemon_identity.py": "886ef37091b1d6279c351c5ae2a62d2e3bf1980bb4c2524da406927918dd06eb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "93894510540c9b6eaa0518e57b09652c7dd08727edbdad57f04e1e4379d211c2",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/executor_reask.py": "8498b4e2963508e2ecff978165917ee218bcfb067238b46e862296c3641b5ba2",
        "scripts/executor_reask_identity.py": "2e68a94bd1c0acba6a5f292568cb8f039bc25932cecb2fc89e809b6f093df7b1",
        "scripts/executor_reask_lifecycle.py": "f7d3dc7b1713bd4dc8d6db8aca0990b3c8bdb186fd24d6ed5902d146a5c08426",
        "scripts/executor_reask_protocol.py": "50256893f881b5c361ea03d12f4a54b89ae8ceb1859f8c939b5022dfcf9a3c4f",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a",
        "scripts/executor_reask_scheduler.py": "d0600c52aabd38c11bff8a50a9e2b07dc720cb82c004ddfca602151e563fb071"
    },
    "0.4.35-e62f35828c7a": {
        "VERSION": "b51c07322891392fdf1955c07b13172c07a6e10529a0ce6dea46559d6247c9ac",
        "scripts/cmux_bridge.py": "8a34327d4031d3f55fc6d7f71f5ec5f8a023f64fafef2eaf1d5c2c78cd52015a",
        "scripts/cmux_daemon_identity.py": "886ef37091b1d6279c351c5ae2a62d2e3bf1980bb4c2524da406927918dd06eb",
        "scripts/cmux_foreground_thread.py": "892cd5f401703e8b7f833cc1dd3a2aba2886fc1859b56b7bed681f5fd92b39b3",
        "scripts/cmux_native_delivery.py": "6a8a936f36a54bacfabd491b1b84f33c8703da1fe2d7400d2e3e345106c49a7d",
        "scripts/cmux_workspace_guard.py": "93894510540c9b6eaa0518e57b09652c7dd08727edbdad57f04e1e4379d211c2",
        "scripts/executor_ready.py": "1eba550c7c8c58a633d77f642d4195699876ff3ecdcec5bacfadf411687f9a79",
        "scripts/executor_reask.py": "8498b4e2963508e2ecff978165917ee218bcfb067238b46e862296c3641b5ba2",
        "scripts/executor_reask_identity.py": "2e68a94bd1c0acba6a5f292568cb8f039bc25932cecb2fc89e809b6f093df7b1",
        "scripts/executor_reask_lifecycle.py": "f7d3dc7b1713bd4dc8d6db8aca0990b3c8bdb186fd24d6ed5902d146a5c08426",
        "scripts/executor_reask_protocol.py": "7c050be2cc7101805e2e60daeba9d17d36f1f258d474432052a2cfa995114337",
        "scripts/executor_reask_scheduler.py": "d0600c52aabd38c11bff8a50a9e2b07dc720cb82c004ddfca602151e563fb071",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reply_advisory.py": "e13c052c8c75cf54682a74666e3cbda70ce797974436b57a585f39b2093b452a"
    },
    "0.4.30-ff75100795a9": {
        "VERSION": "6028dc76386f65f57a4e92607c78ea99f5fa4085426ee6f3c7fe0564dc2b2485",
        "scripts/executor_reask.py": "0a99a8737f689bccbe30e759c94ba9389226c208ab879b2160b7db9c704e0aa4",
        "scripts/executor_ready.py": "c97b560208d741e98fd98eacc2363b130fa05a5e4a544b23bd447fdd764b9ffa",
        "scripts/cmux_bridge.py": "8e5706c42a2e94e99c050670c9beb10549975f486b98d7d53c191bc34f3544c3",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "593acda82846ba68c86f09180fd336c602dc494538cea20649aec96d678993da",
        "scripts/executor_reask_scheduler.py": "e6111f7f969025b8ab3b2e1f883c9bc862c5507048d4b235fc323efbc37f4a28"
    },
    "0.4.31-79ae83e9d5da": {
        "VERSION": "d03897ae78b6351d05a3491c29e362eea73d9bd8d2a53ad38a4e3fda3ef6b9ee",
        "scripts/executor_reask.py": "0a99a8737f689bccbe30e759c94ba9389226c208ab879b2160b7db9c704e0aa4",
        "scripts/executor_ready.py": "c97b560208d741e98fd98eacc2363b130fa05a5e4a544b23bd447fdd764b9ffa",
        "scripts/cmux_bridge.py": "8e5706c42a2e94e99c050670c9beb10549975f486b98d7d53c191bc34f3544c3",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "593acda82846ba68c86f09180fd336c602dc494538cea20649aec96d678993da",
        "scripts/executor_reask_scheduler.py": "e6111f7f969025b8ab3b2e1f883c9bc862c5507048d4b235fc323efbc37f4a28"
    },
    "0.4.28-ae66245771d0": {
        "VERSION": "528e6f9e867bd412b8e69662965d5a534798373abaebfc54a526624796f55e0c",
        "scripts/executor_reask.py": "0a99a8737f689bccbe30e759c94ba9389226c208ab879b2160b7db9c704e0aa4",
        "scripts/executor_ready.py": "c97b560208d741e98fd98eacc2363b130fa05a5e4a544b23bd447fdd764b9ffa",
        "scripts/cmux_bridge.py": "8e5706c42a2e94e99c050670c9beb10549975f486b98d7d53c191bc34f3544c3",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "593acda82846ba68c86f09180fd336c602dc494538cea20649aec96d678993da",
        "scripts/executor_reask_scheduler.py": "e6111f7f969025b8ab3b2e1f883c9bc862c5507048d4b235fc323efbc37f4a28"
    },
    "0.4.32-c7338a400bf4": {
        "VERSION": "3ef61d7b0f5aee9bd2d086657890fe0315f1c836954db4a6858e648bc611eee7",
        "scripts/executor_reask.py": "f7782ae273d5cf2c2944b2372447bae8e85aa93b2da2f0ca5a1944acb39e1e24",
        "scripts/executor_ready.py": "39e297b4ef4b2a1c5977570a81f050482156fd088d6a9cab364acb2e8510af62",
        "scripts/cmux_bridge.py": "8e5706c42a2e94e99c050670c9beb10549975f486b98d7d53c191bc34f3544c3",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "65328f881c2397d1f1e079d7bc129c109b1f4fb7385dfe54fd43a6813030660d",
        "scripts/executor_reask_scheduler.py": "4376f237d8f15652f195bc856cd6ebc17ddfb447387783c1f58cb6b69cd4c901"
    },
    "0.4.33-09de16ceb823": {
        "VERSION": "031f6065d669b8af39b130b3fa5ac7c3fe7683424294c3db1486f8efa21a6585",
        "scripts/executor_reask.py": "f7782ae273d5cf2c2944b2372447bae8e85aa93b2da2f0ca5a1944acb39e1e24",
        "scripts/executor_ready.py": "39e297b4ef4b2a1c5977570a81f050482156fd088d6a9cab364acb2e8510af62",
        "scripts/cmux_bridge.py": "27a8bfac62c21be3910ccf81b50d2d77bbbcb870951e5f367fe846a020ea9b8a",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "65328f881c2397d1f1e079d7bc129c109b1f4fb7385dfe54fd43a6813030660d",
        "scripts/executor_reask_scheduler.py": "4376f237d8f15652f195bc856cd6ebc17ddfb447387783c1f58cb6b69cd4c901"
    },
    "0.4.34-d333557fb498": {
        "VERSION": "1f1673fdef5874a5e9e75a39634b0d001e54cd2fb11b9f3bc21b35de3a204cf9",
        "scripts/executor_reask.py": "f7782ae273d5cf2c2944b2372447bae8e85aa93b2da2f0ca5a1944acb39e1e24",
        "scripts/executor_ready.py": "39e297b4ef4b2a1c5977570a81f050482156fd088d6a9cab364acb2e8510af62",
        "scripts/cmux_bridge.py": "27a8bfac62c21be3910ccf81b50d2d77bbbcb870951e5f367fe846a020ea9b8a",
        "scripts/executor_reply.py": "160f9115be184fb2cd332558c40839db5a61a5ea0cb5e29016740bc488ad6312",
        "scripts/executor_reask_lifecycle.py": "65328f881c2397d1f1e079d7bc129c109b1f4fb7385dfe54fd43a6813030660d",
        "scripts/executor_reask_scheduler.py": "4376f237d8f15652f195bc856cd6ebc17ddfb447387783c1f58cb6b69cd4c901"
    }
}
# Reviewed v3 state/v2 enrollment producers, after full immutable verification.
# A historical issuer can only have enrolled controllers supported when it ran;
# it cannot claim to have enrolled a future CURRENT or a subsequently added release.
LEGACY_FILE_PROTOCOL = {
    '0.4.57-e8685485a9c5': {'descriptor': {'id': 'executor-reask-file-v1',
                    'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
     'controllers': ('0.4.56-bea19f7ff44e',
                     '0.4.54-f86a9efcf10f',
                     '0.4.55-8064e2645b0e',
                     '0.4.53-568efc13c7ae',
                     '0.4.52-0ca4d8002da7',
                     '0.4.51-587295817cef',
                     '0.4.50-3dd2945b5069',
                     '0.4.49-689dda6455be',
                     '0.4.48-3b4304d24f90',
                     '0.4.47-46395e3b21e5',
                     '0.4.46-37e945dea2e3',
                     '0.4.44-2b9bce3b41eb',
                     '0.4.42-5947b2d1f8aa',
                     '0.4.41-28b36490b149',
                     '0.4.37-c7258c383218',
                     '0.4.36-8ab59dbb5a5a',
                     '0.4.35-e62f35828c7a',
                     '0.4.30-ff75100795a9',
                     '0.4.31-79ae83e9d5da',
                     '0.4.28-ae66245771d0',
                     '0.4.32-c7338a400bf4',
                     '0.4.33-09de16ceb823',
                     '0.4.34-d333557fb498',
                     '0.4.57-e8685485a9c5')},
    '0.4.56-bea19f7ff44e': {'descriptor': {'id': 'executor-reask-file-v1',
                                        'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
                         'controllers': ('0.4.54-f86a9efcf10f',
                                         '0.4.55-8064e2645b0e',
                                         '0.4.53-568efc13c7ae',
                                         '0.4.52-0ca4d8002da7',
                                         '0.4.51-587295817cef',
                                         '0.4.50-3dd2945b5069',
                                         '0.4.49-689dda6455be',
                                         '0.4.48-3b4304d24f90',
                                         '0.4.47-46395e3b21e5',
                                         '0.4.46-37e945dea2e3',
                                         '0.4.44-2b9bce3b41eb',
                                         '0.4.42-5947b2d1f8aa',
                                         '0.4.41-28b36490b149',
                                         '0.4.37-c7258c383218',
                                         '0.4.36-8ab59dbb5a5a',
                                         '0.4.35-e62f35828c7a',
                                         '0.4.30-ff75100795a9',
                                         '0.4.31-79ae83e9d5da',
                                         '0.4.28-ae66245771d0',
                                         '0.4.32-c7338a400bf4',
                                         '0.4.33-09de16ceb823',
                                         '0.4.34-d333557fb498',
                                         '0.4.56-bea19f7ff44e')},
    '0.4.54-f86a9efcf10f': {'descriptor': {'id': 'executor-reask-file-v1',
                                        'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
                         'controllers': ('0.4.53-568efc13c7ae',
                                         '0.4.52-0ca4d8002da7',
                                         '0.4.51-587295817cef',
                                         '0.4.50-3dd2945b5069',
                                         '0.4.49-689dda6455be',
                                         '0.4.48-3b4304d24f90',
                                         '0.4.47-46395e3b21e5',
                                         '0.4.46-37e945dea2e3',
                                         '0.4.44-2b9bce3b41eb',
                                         '0.4.42-5947b2d1f8aa',
                                         '0.4.41-28b36490b149',
                                         '0.4.37-c7258c383218',
                                         '0.4.36-8ab59dbb5a5a',
                                         '0.4.35-e62f35828c7a',
                                         '0.4.30-ff75100795a9',
                                         '0.4.31-79ae83e9d5da',
                                         '0.4.28-ae66245771d0',
                                         '0.4.32-c7338a400bf4',
                                         '0.4.33-09de16ceb823',
                                         '0.4.34-d333557fb498',
                                         '0.4.54-f86a9efcf10f')},
 '0.4.55-8064e2645b0e': {'descriptor': {'id': 'executor-reask-file-v1',
                                        'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
                         'controllers': ('0.4.53-568efc13c7ae',
                                         '0.4.52-0ca4d8002da7',
                                         '0.4.51-587295817cef',
                                         '0.4.50-3dd2945b5069',
                                         '0.4.49-689dda6455be',
                                         '0.4.48-3b4304d24f90',
                                         '0.4.47-46395e3b21e5',
                                         '0.4.46-37e945dea2e3',
                                         '0.4.44-2b9bce3b41eb',
                                         '0.4.42-5947b2d1f8aa',
                                         '0.4.41-28b36490b149',
                                         '0.4.37-c7258c383218',
                                         '0.4.36-8ab59dbb5a5a',
                                         '0.4.35-e62f35828c7a',
                                         '0.4.30-ff75100795a9',
                                         '0.4.31-79ae83e9d5da',
                                         '0.4.28-ae66245771d0',
                                         '0.4.32-c7338a400bf4',
                                         '0.4.33-09de16ceb823',
                                         '0.4.34-d333557fb498',
                                         '0.4.55-8064e2645b0e')},
    '0.4.53-568efc13c7ae': {'descriptor': {'id': 'executor-reask-file-v1',
                                            'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
                             'controllers': ('0.4.52-0ca4d8002da7',
                                             '0.4.51-587295817cef',
                                             '0.4.50-3dd2945b5069',
                                             '0.4.49-689dda6455be',
                                             '0.4.48-3b4304d24f90',
                                             '0.4.47-46395e3b21e5',
                                             '0.4.46-37e945dea2e3',
                                             '0.4.44-2b9bce3b41eb',
                                             '0.4.42-5947b2d1f8aa',
                                             '0.4.41-28b36490b149',
                                             '0.4.37-c7258c383218',
                                             '0.4.36-8ab59dbb5a5a',
                                             '0.4.35-e62f35828c7a',
                                             '0.4.30-ff75100795a9',
                                             '0.4.31-79ae83e9d5da',
                                             '0.4.28-ae66245771d0',
                                             '0.4.32-c7338a400bf4',
                                             '0.4.33-09de16ceb823',
                                             '0.4.34-d333557fb498',
                                             '0.4.53-568efc13c7ae')},
    '0.4.52-0ca4d8002da7': {'descriptor': {'id': 'executor-reask-file-v1',
                                            'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
                             'controllers': ('0.4.51-587295817cef',
                                             '0.4.50-3dd2945b5069',
                                             '0.4.49-689dda6455be',
                                             '0.4.48-3b4304d24f90',
                                             '0.4.47-46395e3b21e5',
                                             '0.4.46-37e945dea2e3',
                                             '0.4.44-2b9bce3b41eb',
                                             '0.4.42-5947b2d1f8aa',
                                             '0.4.41-28b36490b149',
                                             '0.4.37-c7258c383218',
                                             '0.4.36-8ab59dbb5a5a',
                                             '0.4.35-e62f35828c7a',
                                             '0.4.30-ff75100795a9',
                                             '0.4.31-79ae83e9d5da',
                                             '0.4.28-ae66245771d0',
                                             '0.4.32-c7338a400bf4',
                                             '0.4.33-09de16ceb823',
                                             '0.4.34-d333557fb498',
                                             '0.4.52-0ca4d8002da7')},
    '0.4.51-587295817cef': {'descriptor': {'id': 'executor-reask-file-v1',
                                            'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
                             'controllers': ('0.4.50-3dd2945b5069',
                                             '0.4.49-689dda6455be',
                                             '0.4.48-3b4304d24f90',
                                             '0.4.47-46395e3b21e5',
                                             '0.4.46-37e945dea2e3',
                                             '0.4.44-2b9bce3b41eb',
                                             '0.4.42-5947b2d1f8aa',
                                             '0.4.41-28b36490b149',
                                             '0.4.37-c7258c383218',
                                             '0.4.36-8ab59dbb5a5a',
                                             '0.4.35-e62f35828c7a',
                                             '0.4.30-ff75100795a9',
                                             '0.4.31-79ae83e9d5da',
                                             '0.4.28-ae66245771d0',
                                             '0.4.32-c7338a400bf4',
                                             '0.4.33-09de16ceb823',
                                             '0.4.34-d333557fb498',
                                             '0.4.51-587295817cef')},
    "0.4.50-3dd2945b5069": {
        "descriptor": {
            "id": "executor-reask-file-v1",
            "sha256": "af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647",
        },
        "controllers": (
            "0.4.28-ae66245771d0",
            "0.4.30-ff75100795a9",
            "0.4.31-79ae83e9d5da",
            "0.4.32-c7338a400bf4",
            "0.4.33-09de16ceb823",
            "0.4.34-d333557fb498",
            "0.4.35-e62f35828c7a",
            "0.4.36-8ab59dbb5a5a",
            "0.4.37-c7258c383218",
            "0.4.41-28b36490b149",
            "0.4.42-5947b2d1f8aa",
            "0.4.44-2b9bce3b41eb",
            "0.4.46-37e945dea2e3",
            "0.4.47-46395e3b21e5",
            "0.4.48-3b4304d24f90",
            "0.4.49-689dda6455be",
            "0.4.50-3dd2945b5069",
        ),
    },
    "0.4.49-689dda6455be": {
        "descriptor": {
            "id": "executor-reask-file-v1",
            "sha256": "af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647",
        },
        "controllers": (
            "0.4.28-ae66245771d0",
            "0.4.30-ff75100795a9",
            "0.4.31-79ae83e9d5da",
            "0.4.32-c7338a400bf4",
            "0.4.33-09de16ceb823",
            "0.4.34-d333557fb498",
            "0.4.35-e62f35828c7a",
            "0.4.36-8ab59dbb5a5a",
            "0.4.37-c7258c383218",
            "0.4.41-28b36490b149",
            "0.4.42-5947b2d1f8aa",
            "0.4.44-2b9bce3b41eb",
            "0.4.46-37e945dea2e3",
            "0.4.47-46395e3b21e5",
            "0.4.48-3b4304d24f90",
            "0.4.49-689dda6455be",
        ),
    },
    '0.4.48-3b4304d24f90': {'descriptor': {'id': 'executor-reask-file-v1',
                                            'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
                             'controllers': ('0.4.28-ae66245771d0',
                                             '0.4.30-ff75100795a9',
                                             '0.4.31-79ae83e9d5da',
                                             '0.4.32-c7338a400bf4',
                                             '0.4.33-09de16ceb823',
                                             '0.4.34-d333557fb498',
                                             '0.4.35-e62f35828c7a',
                                             '0.4.36-8ab59dbb5a5a',
                                             '0.4.37-c7258c383218',
                                             '0.4.41-28b36490b149',
                                             '0.4.42-5947b2d1f8aa',
                                             '0.4.44-2b9bce3b41eb',
                                             '0.4.46-37e945dea2e3',
                                             '0.4.47-46395e3b21e5',
                                             '0.4.48-3b4304d24f90')},
    "0.4.47-46395e3b21e5": {
        "descriptor": {
            "id": "executor-reask-file-v1",
            "sha256": "af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647",
        },
        "controllers": (
            "0.4.28-ae66245771d0",
            "0.4.30-ff75100795a9",
            "0.4.31-79ae83e9d5da",
            "0.4.32-c7338a400bf4",
            "0.4.33-09de16ceb823",
            "0.4.34-d333557fb498",
            "0.4.35-e62f35828c7a",
            "0.4.36-8ab59dbb5a5a",
            "0.4.37-c7258c383218",
            "0.4.41-28b36490b149",
            "0.4.42-5947b2d1f8aa",
            "0.4.44-2b9bce3b41eb",
            "0.4.46-37e945dea2e3",
            "0.4.47-46395e3b21e5",
        ),
    },
    "0.4.46-37e945dea2e3": {
        "descriptor": {
            "id": "executor-reask-file-v1",
            "sha256": "af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647",
        },
        "controllers": (
            "0.4.28-ae66245771d0",
            "0.4.30-ff75100795a9",
            "0.4.31-79ae83e9d5da",
            "0.4.32-c7338a400bf4",
            "0.4.33-09de16ceb823",
            "0.4.34-d333557fb498",
            "0.4.35-e62f35828c7a",
            "0.4.36-8ab59dbb5a5a",
            "0.4.37-c7258c383218",
            "0.4.41-28b36490b149",
            "0.4.42-5947b2d1f8aa",
            "0.4.44-2b9bce3b41eb",
            "0.4.46-37e945dea2e3",
        ),
    },
    "0.4.44-2b9bce3b41eb": {
        "descriptor": {
            "id": "executor-reask-file-v1",
            "sha256": "af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647",
        },
        "controllers": (
            "0.4.28-ae66245771d0",
            "0.4.30-ff75100795a9",
            "0.4.31-79ae83e9d5da",
            "0.4.32-c7338a400bf4",
            "0.4.33-09de16ceb823",
            "0.4.34-d333557fb498",
            "0.4.35-e62f35828c7a",
            "0.4.36-8ab59dbb5a5a",
            "0.4.37-c7258c383218",
            "0.4.41-28b36490b149",
            "0.4.42-5947b2d1f8aa",
            "0.4.44-2b9bce3b41eb",
        ),
    },
    '0.4.42-5947b2d1f8aa': {
        'descriptor': {'id': 'executor-reask-file-v1',
                       'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
        'controllers': (
            '0.4.28-ae66245771d0',
            '0.4.30-ff75100795a9',
            '0.4.31-79ae83e9d5da',
            '0.4.32-c7338a400bf4',
            '0.4.33-09de16ceb823',
            '0.4.34-d333557fb498',
            '0.4.35-e62f35828c7a',
            '0.4.36-8ab59dbb5a5a',
            '0.4.37-c7258c383218',
            '0.4.41-28b36490b149',
            '0.4.42-5947b2d1f8aa',
        ),
    },
    '0.4.41-28b36490b149': {
        'descriptor': {'id': 'executor-reask-file-v1',
                       'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
        'controllers': ('0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
                        '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
                        '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
                        '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a',
                        '0.4.37-c7258c383218', '0.4.41-28b36490b149'),
    },
    '0.4.37-c7258c383218': {
        'descriptor': {'id': 'executor-reask-file-v1',
                       'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
        'controllers': ('0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
                        '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
                        '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
                        '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a',
                        '0.4.37-c7258c383218'),
    },
    '0.4.36-8ab59dbb5a5a': {
        'descriptor': {'id': 'executor-reask-file-v1',
                       'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
        'controllers': ('0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
                        '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
                        '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
                        '0.4.35-e62f35828c7a', '0.4.36-8ab59dbb5a5a'),
    },
    '0.4.35-e62f35828c7a': {
        'descriptor': {'id': 'executor-reask-file-v1',
                       'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
        'controllers': ('0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
                        '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
                        '0.4.33-09de16ceb823', '0.4.34-d333557fb498',
                        '0.4.35-e62f35828c7a'),
    },
    '0.4.32-c7338a400bf4': {
        'descriptor': {'id': 'executor-reask-file-v1',
                       'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
        'controllers': ('0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
                        '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4'),
    },
    '0.4.33-09de16ceb823': {
        'descriptor': {'id': 'executor-reask-file-v1',
                       'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
        'controllers': ('0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
                        '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
                        '0.4.33-09de16ceb823'),
    },
    '0.4.34-d333557fb498': {
        'descriptor': {'id': 'executor-reask-file-v1',
                       'sha256': 'af1edf631c48edfe62cfd6a1beeab6c03d8ffe5189f4f49f6c5689dd666be647'},
        'controllers': ('0.4.28-ae66245771d0', '0.4.30-ff75100795a9',
                        '0.4.31-79ae83e9d5da', '0.4.32-c7338a400bf4',
                        '0.4.33-09de16ceb823', '0.4.34-d333557fb498'),
    },
}
# Historical abilities are explicit and never inherit future FILES changes.
LEGACY_CAPABILITIES = {
    '0.4.57-e8685485a9c5': {'controller_files': ('VERSION',
                          'scripts/executor_reask.py',
                          'scripts/executor_ready.py',
                          'scripts/cmux_bridge.py',
                          'scripts/executor_reply.py',
                          'scripts/executor_reask_lifecycle.py',
                          'scripts/executor_reask_identity.py',
                          'scripts/executor_reask_protocol.py',
                          'scripts/executor_reply_advisory.py',
                          'scripts/cmux_native_delivery.py',
                          'scripts/cmux_workspace_guard.py',
                          'scripts/cmux_daemon_identity.py',
                          'scripts/cmux_foreground_thread.py',
                          'scripts/cmux_coordination_paths.py',
                          'scripts/executor_proactive_hook.py',
                          'scripts/native_user_record.py'),
     'reservation_inquiries': True,
     'proactive_hook': True},
    '0.4.56-bea19f7ff44e': {'controller_files': ('VERSION',
                                              'scripts/executor_reask.py',
                                              'scripts/executor_ready.py',
                                              'scripts/cmux_bridge.py',
                                              'scripts/executor_reply.py',
                                              'scripts/executor_reask_lifecycle.py',
                                              'scripts/executor_reask_identity.py',
                                              'scripts/executor_reask_protocol.py',
                                              'scripts/executor_reply_advisory.py',
                                              'scripts/cmux_native_delivery.py',
                                              'scripts/cmux_workspace_guard.py',
                                              'scripts/cmux_daemon_identity.py',
                                              'scripts/cmux_foreground_thread.py',
                                              'scripts/cmux_coordination_paths.py',
                                              'scripts/executor_proactive_hook.py',
                                              'scripts/native_user_record.py'),
                         'reservation_inquiries': True,
                         'proactive_hook': True},
    '0.4.54-f86a9efcf10f': {'controller_files': ('VERSION',
                                              'scripts/executor_reask.py',
                                              'scripts/executor_ready.py',
                                              'scripts/cmux_bridge.py',
                                              'scripts/executor_reply.py',
                                              'scripts/executor_reask_lifecycle.py',
                                              'scripts/executor_reask_identity.py',
                                              'scripts/executor_reask_protocol.py',
                                              'scripts/executor_reply_advisory.py',
                                              'scripts/cmux_native_delivery.py',
                                              'scripts/cmux_workspace_guard.py',
                                              'scripts/cmux_daemon_identity.py',
                                              'scripts/cmux_foreground_thread.py',
                                              'scripts/cmux_coordination_paths.py'),
                         'reservation_inquiries': True},
 '0.4.55-8064e2645b0e': {'controller_files': ('VERSION',
                                              'scripts/executor_reask.py',
                                              'scripts/executor_ready.py',
                                              'scripts/cmux_bridge.py',
                                              'scripts/executor_reply.py',
                                              'scripts/executor_reask_lifecycle.py',
                                              'scripts/executor_reask_identity.py',
                                              'scripts/executor_reask_protocol.py',
                                              'scripts/executor_reply_advisory.py',
                                              'scripts/cmux_native_delivery.py',
                                              'scripts/cmux_workspace_guard.py',
                                              'scripts/cmux_daemon_identity.py',
                                              'scripts/cmux_foreground_thread.py',
                                              'scripts/cmux_coordination_paths.py'),
                         'reservation_inquiries': True},
    '0.4.53-568efc13c7ae': {'controller_files': ('VERSION',
                                                  'scripts/executor_reask.py',
                                                  'scripts/executor_ready.py',
                                                  'scripts/cmux_bridge.py',
                                                  'scripts/executor_reply.py',
                                                  'scripts/executor_reask_lifecycle.py',
                                                  'scripts/executor_reask_identity.py',
                                                  'scripts/executor_reask_protocol.py',
                                                  'scripts/executor_reply_advisory.py',
                                                  'scripts/cmux_native_delivery.py',
                                                  'scripts/cmux_workspace_guard.py',
                                                  'scripts/cmux_daemon_identity.py',
                                                  'scripts/cmux_foreground_thread.py',
                                                  'scripts/cmux_coordination_paths.py'),
                             'reservation_inquiries': True},
    '0.4.52-0ca4d8002da7': {'controller_files': ('VERSION',
                                                  'scripts/executor_reask.py',
                                                  'scripts/executor_ready.py',
                                                  'scripts/cmux_bridge.py',
                                                  'scripts/executor_reply.py',
                                                  'scripts/executor_reask_lifecycle.py',
                                                  'scripts/executor_reask_identity.py',
                                                  'scripts/executor_reask_protocol.py',
                                                  'scripts/executor_reply_advisory.py',
                                                  'scripts/cmux_native_delivery.py',
                                                  'scripts/cmux_workspace_guard.py',
                                                  'scripts/cmux_daemon_identity.py',
                                                  'scripts/cmux_foreground_thread.py',
                                                  'scripts/cmux_coordination_paths.py'),
                             'reservation_inquiries': True},
    '0.4.51-587295817cef': {'controller_files': ('VERSION',
                                                  'scripts/executor_reask.py',
                                                  'scripts/executor_ready.py',
                                                  'scripts/cmux_bridge.py',
                                                  'scripts/executor_reply.py',
                                                  'scripts/executor_reask_lifecycle.py',
                                                  'scripts/executor_reask_identity.py',
                                                  'scripts/executor_reask_protocol.py',
                                                  'scripts/executor_reply_advisory.py',
                                                  'scripts/cmux_native_delivery.py',
                                                  'scripts/cmux_workspace_guard.py',
                                                  'scripts/cmux_daemon_identity.py',
                                                  'scripts/cmux_foreground_thread.py',
                                                  'scripts/cmux_coordination_paths.py'),
                             'reservation_inquiries': True},
    "0.4.50-3dd2945b5069": {
        "controller_files": (
            "VERSION",
            "scripts/executor_reask.py",
            "scripts/executor_ready.py",
            "scripts/cmux_bridge.py",
            "scripts/executor_reply.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply_advisory.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_coordination_paths.py",
        ),
        "reservation_inquiries": True,
    },
    "0.4.49-689dda6455be": {
        "controller_files": (
            "VERSION",
            "scripts/executor_reask.py",
            "scripts/executor_ready.py",
            "scripts/cmux_bridge.py",
            "scripts/executor_reply.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply_advisory.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_coordination_paths.py",
        ),
        "reservation_inquiries": True,
    },
    '0.4.48-3b4304d24f90': {'controller_files': ('VERSION',
                                                  'scripts/executor_reask.py',
                                                  'scripts/executor_ready.py',
                                                  'scripts/cmux_bridge.py',
                                                  'scripts/executor_reply.py',
                                                  'scripts/executor_reask_lifecycle.py',
                                                  'scripts/executor_reask_identity.py',
                                                  'scripts/executor_reask_protocol.py',
                                                  'scripts/executor_reply_advisory.py',
                                                  'scripts/cmux_native_delivery.py',
                                                  'scripts/cmux_workspace_guard.py',
                                                  'scripts/cmux_daemon_identity.py',
                                                  'scripts/cmux_foreground_thread.py',
                                                  'scripts/cmux_coordination_paths.py'),
                             'reservation_inquiries': True},
    "0.4.47-46395e3b21e5": {
        "controller_files": (
            "VERSION",
            "scripts/executor_reask.py",
            "scripts/executor_ready.py",
            "scripts/cmux_bridge.py",
            "scripts/executor_reply.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply_advisory.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_coordination_paths.py",
        ),
        "reservation_inquiries": True,
    },
    "0.4.46-37e945dea2e3": {
        "controller_files": (
            "VERSION",
            "scripts/executor_reask.py",
            "scripts/executor_ready.py",
            "scripts/cmux_bridge.py",
            "scripts/executor_reply.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply_advisory.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_coordination_paths.py",
        ),
        "reservation_inquiries": True,
    },
    "0.4.44-2b9bce3b41eb": {
        "controller_files": (
            "VERSION",
            "scripts/cmux_bridge.py",
            "scripts/cmux_coordination_paths.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/executor_ready.py",
            "scripts/executor_reask.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply.py",
            "scripts/executor_reply_advisory.py",
        ),
        "reservation_inquiries": True,
    },
    '0.4.42-5947b2d1f8aa': {
        'controller_files': (
            "VERSION",
            "scripts/cmux_bridge.py",
            "scripts/cmux_coordination_paths.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/executor_ready.py",
            "scripts/executor_reask.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply.py",
            "scripts/executor_reply_advisory.py",
        ),
        'reservation_inquiries': True,
    },
    '0.4.41-28b36490b149': {
        'controller_files': (
            "VERSION",
            "scripts/cmux_bridge.py",
            "scripts/cmux_coordination_paths.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/executor_ready.py",
            "scripts/executor_reask.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply.py",
            "scripts/executor_reply_advisory.py",
        ),
        'reservation_inquiries': True,
    },
    '0.4.37-c7258c383218': {
        'controller_files': (
            "VERSION",
            "scripts/cmux_bridge.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/executor_ready.py",
            "scripts/executor_reask.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply.py",
            "scripts/executor_reply_advisory.py",
        ),
        'reservation_inquiries': True,
    },
    '0.4.36-8ab59dbb5a5a': {
        'controller_files': (
            "VERSION",
            "scripts/cmux_bridge.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/executor_ready.py",
            "scripts/executor_reask.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply.py",
            "scripts/executor_reply_advisory.py",
        ),
        'reservation_inquiries': True,
    },
    '0.4.35-e62f35828c7a': {
        'controller_files': (
            "VERSION",
            "scripts/cmux_bridge.py",
            "scripts/cmux_daemon_identity.py",
            "scripts/cmux_foreground_thread.py",
            "scripts/cmux_native_delivery.py",
            "scripts/cmux_workspace_guard.py",
            "scripts/executor_ready.py",
            "scripts/executor_reask.py",
            "scripts/executor_reask_identity.py",
            "scripts/executor_reask_lifecycle.py",
            "scripts/executor_reask_protocol.py",
            "scripts/executor_reply.py",
            "scripts/executor_reply_advisory.py",
        ),
        'reservation_inquiries': True,
    },
}


def historical_files(name):
    return LEGACY_CAPABILITIES.get(name, {}).get('controller_files', LEGACY_FILES)


CONTRACT = dict(id='executor-reask-file-v1', version=1,
                operations=['bound-file-request', 'bound-file-reply-detection',
                            'authorization-stop', 'formal-reservation'],
                forbidden=['transport', 'transcript', 'idle-binding', 'consume',
                           'pending-reconciliation', 'old-code-execution'],
                controller='immutable-original', terminal_pending='preserve-verbatim')


def descriptor():
    raw = json.dumps(CONTRACT, sort_keys=True, separators=(',', ':')).encode()
    return dict(id=CONTRACT['id'], sha256=hashlib.sha256(raw).hexdigest())


def _descriptor_producer(release):
    return (release['kind'] == 'current'
            or LEGACY_FILE_PROTOCOL.get(release.get('name'), {}).get('descriptor') == descriptor())


def state_controller(state, record=None):
    """Return the complete original identity, or None; never repair missing pins."""
    if not isinstance(state, dict) or record is not None and not isinstance(record, dict):
        return None
    candidates = []
    if 'controller' in state:
        candidates.append(state['controller'])
    elif state.get('version') == 3:
        return None
    continuation = state.get('continuation')
    if continuation is not None:
        if not continuation_shape(continuation, state):
            return None
        candidates.append(continuation['controller'])
    if record is not None:
        candidates.append(record.get('controller'))
    if (not candidates or not all(controller_shape(v) for v in candidates)
            or any(value != candidates[0] for value in candidates[1:])):
        return None
    return copy.deepcopy(candidates[0])


def controller_diagnostic(original, invoked):
    """Describe the rejected invocation; recorded pins grant no execution authority."""
    return dict(original_identity='RECORDED' if original is not None else 'UNKNOWN',
                original_controller=copy.deepcopy(original),
                invoked_controller=copy.deepcopy(invoked),
                next_action=('USE_ORIGINAL_CONTROLLER' if original is not None
                             else 'INSPECT_ORIGINAL_EVIDENCE'))


def sha256_shape(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def canonical_path(value):
    return (isinstance(value, str) and bool(value)
            and Path(value).is_absolute() and str(Path(value)) == value
            and '..' not in Path(value).parts and Path(value).resolve() == Path(value))


def controller_shape(value):
    try:
        return (isinstance(value, dict) and set(value) == {'source', 'python', 'files'}
                and canonical_path(value['source']) and canonical_path(value['python'])
                and isinstance(value['files'], dict)
                and set(value['files']) in (
                    set(FILES), set(LEGACY_FILES),
                    *(set(capability['controller_files'])
                      for capability in LEGACY_CAPABILITIES.values()))
                and all(sha256_shape(v) for v in value['files'].values()))
    except (TypeError, KeyError, ValueError, OSError):
        return False


def continuation_shape(value, state):
    """Validate identity structure only; expiry/revocation remain caller gates."""
    def text(v, limit):
        return isinstance(v, str) and bool(v.strip()) and len(v) <= limit

    def finite(v):
        return type(v) in (int, float) and math.isfinite(v)

    try:
        if not isinstance(value, dict):
            return False
        pin, until, authorized = value['authorization'], value['until_epoch'], value['authorized_epoch']
        return (value.get('enabled') is True and text(value.get('id'), 128)
                and ('reservation_inquiries' not in value
                     or type(value['reservation_inquiries']) is bool)
                and ('proactive_hook' not in value
                     or type(value['proactive_hook']) is bool)
                and (value.get('proactive_hook') is not True
                     or value.get('reservation_inquiries') is True)
                and text(value.get('overall_task'), 4096) and text(value.get('status'), 128)
                and text(value.get('owner'), 128) and value['owner'] == state.get('supervisor')
                and finite(authorized) and (until is None or finite(until) and until > authorized)
                and isinstance(pin, dict) and set(pin) == {'path', 'sha256', 'bytes'}
                and canonical_path(pin['path']) and sha256_shape(pin['sha256'])
                and type(pin['bytes']) is int and 0 < pin['bytes'] <= 65536
                and controller_shape(value.get('controller')))
    except (TypeError, KeyError, ValueError, OSError, OverflowError):
        return False


def release_name(value):
    if not controller_shape(value):
        return None
    source = Path(value['source'])
    base = Path.home() / '.local/share/multi-agent-collaboration/releases'
    if source.name != 'source' or source.parent.parent != base:
        return None
    return source.parent.name


def legacy_controller(value):
    try:
        return (release_name(value) in LEGACY
                and _controller_release(value, {})['kind'] == 'legacy')
    except (TypeError, KeyError, ValueError, OSError):
        return False


def scheduler_shape(value):
    try:
        return (isinstance(value, dict) and set(value) == {'path', 'sha256', 'python', 'label'}
                and value['label'] == LABEL and canonical_path(value['path'])
                and canonical_path(value['python'])
                and Path(value['path']).parts[-2:] == ('scripts', 'executor_reask_scheduler.py')
                and sha256_shape(value['sha256']))
    except (TypeError, KeyError, ValueError, OSError):
        return False


_STAT_FIELDS = ('st_dev', 'st_ino', 'st_size', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')


def _stamp(info):
    return tuple(getattr(info, key) for key in _STAT_FIELDS)


class _ReleaseSnapshot:
    def __init__(self, root):
        self.root = root
        self.files = {}

    def record(self, path, info):
        stamp = _stamp(info)
        if self.files.setdefault(path, stamp) != stamp:
            raise ValueError('release changed during verification')
        for parent in path.parents:
            if parent == self.root.parent or parent in self.files:
                break
            info = parent.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('release parent is not a directory')
            self.files[parent] = _stamp(info)

    def unchanged(self):
        try:
            return all(_stamp(path.stat(follow_symlinks=False)) == stamp
                       for path, stamp in self.files.items())
        except OSError:
            return False


class ReleaseVerification:
    """One sweep's verified bytes, reused only while every member is unchanged.

    Never persist this object or reuse it across sweeps. Binding, authorization,
    enrollment and stop checks remain live; this stores release files only.
    """
    def __init__(self):
        self._releases = {}
        self.full_verifications = 0
        self.reuses = 0
        self.invalidations = 0

    def check(self, source):
        entry = self._releases.get(source)
        if entry is not None:
            result, snapshot = entry
            if snapshot.unchanged():
                self.reuses += 1
                return result
            del self._releases[source]
            self.invalidations += 1
        snapshot = _ReleaseSnapshot(source.parent)
        self.full_verifications += 1
        result = _load_trusted_release(source, snapshot)
        if not snapshot.unchanged():
            raise ValueError('release changed during verification')
        self._releases[source] = result, snapshot
        return result

    def summary(self):
        return dict(full_verifications=self.full_verifications, reuses=self.reuses,
                    invalidations=self.invalidations, scope='ONE_SWEEP')


def _read_regular(path, limit, *, readonly=True, executable=None, observation=None):
    """Bounded descriptor read; reject aliases, special files and changing pins."""
    if not canonical_path(str(path)):
        raise ValueError('noncanonical release file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                or readonly and before.st_mode & 0o222
                or executable is not None and bool(before.st_mode & 0o111) != executable):
            raise ValueError('release file is not bounded, regular and read-only')
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        named = path.stat(follow_symlinks=False)
        if (len(raw) != before.st_size or len(raw) > limit
                or any(getattr(before, k) != getattr(after, k)
                       or getattr(after, k) != getattr(named, k) for k in _STAT_FIELDS)):
            raise ValueError('release file changed during verification')
        if observation is not None:
            observation.record(path, after)
        return raw


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate manifest field')
        result[key] = value
    return result


def _read_object(path, *, readonly=True, observation=None):
    raw = _read_regular(path, MAX_MANIFEST_BYTES, readonly=readonly, observation=observation)
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError('release metadata must be an object')
    return raw, value


def _verify_release(source, expected, observation=None):
    if not sha256_shape(expected):
        raise ValueError('missing trusted manifest digest')
    manifest = source.parent / 'MANIFEST.json'
    raw, value = _read_object(manifest, observation=observation)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('untrusted release manifest')
    files = value.get('files')
    required = {*LEGACY_FILES, SCHEDULER, 'scripts/configuration_guardian.py', 'scripts/manage_install.py'}
    if (not isinstance(files, dict) or not 1 <= len(files) <= MAX_RELEASE_FILES
            or not required.issubset(files)):
        raise ValueError('incomplete release manifest')
    total = 0
    for name, pin in files.items():
        member = Path(name)
        if (not name or member.is_absolute() or '..' in member.parts
                or str(member) != name or name == '.' or not isinstance(pin, dict)
                or set(pin) != {'bytes', 'sha256', 'executable'}
                or type(pin['bytes']) is not int or pin['bytes'] < 0
                or type(pin['executable']) is not bool or not sha256_shape(pin['sha256'])):
            raise ValueError('invalid release member pin')
        total += pin['bytes']
        if total > MAX_RELEASE_BYTES:
            raise ValueError('release verification budget exceeded')
        content = _read_regular(source / member, pin['bytes'], executable=pin['executable'],
                                observation=observation)
        if len(content) != pin['bytes'] or hashlib.sha256(content).hexdigest() != pin['sha256']:
            raise ValueError('release member changed')
    if _read_regular(manifest, MAX_MANIFEST_BYTES, observation=observation) != raw:
        raise ValueError('manifest changed during verification')
    return files


def _trusted_release(source, checked):
    """Per-call reuse, or a sweep snapshot checked for mutation on every use."""
    if not canonical_path(str(source)):
        raise ValueError('noncanonical release source')
    if isinstance(checked, ReleaseVerification):
        return checked.check(source)
    if source in checked:
        return checked[source]
    result = _load_trusted_release(source)
    checked[source] = result
    return result


def _load_trusted_release(source, observation=None):
    if source == Path(__file__).resolve().parents[1]:
        # The scheduler owns CURRENT's full approved_release gate under its
        # locks. Check the actual local controller/provenance, also in writable
        # test checkouts; this fast path is not an independent release approval.
        files, total = {}, 0
        for name in (*FILES, SCHEDULER):
            raw = _read_regular(source / name, MAX_RELEASE_BYTES - total, readonly=False,
                                observation=observation)
            total += len(raw)
            files[name] = dict(sha256=hashlib.sha256(raw).hexdigest())
        result = dict(kind='current', files=files, python=str(Path(sys.executable).resolve()))
        return result
    base = Path.home() / '.local/share/multi-agent-collaboration/releases'
    if source.name != 'source' or source.parent.parent != base:
        raise ValueError('release is outside the immutable layout')
    name = source.parent.name
    if name not in LEGACY:
        raise ValueError('unknown historical release is unsupported')
    files = _verify_release(source, LEGACY_MANIFESTS.get(name), observation)
    if {key: files[key]['sha256'] for key in (*historical_files(name), SCHEDULER)} != LEGACY[name]:
        raise ValueError('unreviewed controller/scheduler combination')
    result = dict(kind='legacy', name=name, files=files)
    return result


def _controller_release(value, checked):
    if not controller_shape(value):
        raise ValueError('invalid controller')
    release = _trusted_release(Path(value['source']), checked)
    members = FILES if release['kind'] == 'current' else historical_files(release['name'])
    if (value['files'] != {key: release['files'][key]['sha256'] for key in members}
            or release['kind'] == 'current' and value['python'] != release['python']):
        raise ValueError('controller pins differ from the trusted release')
    return release


def _supported_state(state, record, checked):
    value = state_controller(state, record)
    if value is None or type(state.get('version')) is not int or state['version'] not in (2, 3):
        return False
    release = _controller_release(value, checked)
    if ('proactive_hook' in (state.get('continuation') or {})
            and release['kind'] != 'current'
            and not LEGACY_CAPABILITIES.get(release['name'], {}).get('proactive_hook')):
        return False  # only an explicitly reviewed producer granted this capability
    if ('reservation_inquiries' in (state.get('continuation') or {})
            and release['kind'] != 'current'
            and not LEGACY_CAPABILITIES.get(release['name'], {}).get('reservation_inquiries')):
        return False
    # Old v2 producers and the explicitly reviewed v3 producer have different
    # schemas. A public descriptor never grants support to an unknown release.
    if state['version'] == 2:
        return release['kind'] == 'current' or release['name'] not in LEGACY_FILE_PROTOCOL
    return _descriptor_producer(release) and state.get('file_protocol') == descriptor()


def supports_state(state, record=None, *, verification=None):
    """Compatibility only; caller gates CURRENT approval, binding, stop/expiry."""
    try:
        return bool(_supported_state(state, record, verification if verification is not None else {}))
    except (TypeError, KeyError, ValueError, OSError, RecursionError):
        return False


def supports_record(record, state, current_scheduler, *, verification=None):
    """Verify producer pins, not foreground authentication or enrollment CAS."""
    try:
        checked = verification if verification is not None else {}
        if not _supported_state(state, record, checked):
            return False
        controller, issuer = record['controller'], record.get('scheduler')
        if not scheduler_shape(issuer):
            return False
        source = Path(issuer['path']).parents[1]
        release = _trusted_release(source, checked)
        if (issuer['sha256'] != release['files'][SCHEDULER]['sha256']
                or release['kind'] == 'current' and
                (issuer != current_scheduler or issuer['python'] != release['python'])):
            return False
        same_origin = (str(source) == controller['source']
                       and issuer['python'] == controller['python'])
        if record.get('schema') == 'executor-reask-enrollment-v1':
            return same_origin and release.get('name') not in LEGACY_FILE_PROTOCOL
        if (record.get('schema') != 'executor-reask-enrollment-v2'
                or record.get('file_protocol') != descriptor() or not _descriptor_producer(release)):
            return False
        if (release['kind'] == 'legacy' and release_name(controller)
                not in LEGACY_FILE_PROTOCOL[release['name']]['controllers']):
            return False
        # A reviewed original issuer may remain on its original enrollment.
        # Fresh registration still requires CURRENT and its explicit live/CAS
        # checks; compatibility alone neither registers nor migrates a record.
        return (record.get('authorization') == 'EXPLICIT_REASK_ENROLL_EXISTING'
                or record.get('authorization') == 'EXPLICIT_REASK_START' and same_origin)
    except (TypeError, KeyError, ValueError, OSError, RecursionError):
        return False
