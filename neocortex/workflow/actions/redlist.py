"""Deterministic Corpus redlist policy.

The redlist is deliberately metadata-only.  It is evaluated against a
``FileSnapshot`` before duplicate planning, content-type detection, or route
execution.  A match is a policy decision, not an external classifier result.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REDLIST_POLICY_SCHEMA = "neocortex.corpus-redlist/v2"

# Source of truth requested by Víctor.  Entries beginning with a dot are either
# exact dot-file names or case-insensitive suffixes.  Keeping the literal list
# here makes the policy auditable and avoids heuristic classification.
_REDLIST_TEXT = """
.0
.0-0
.0-x86
.0_loc
.0_ cu
.0-2f9188b68640dbf72295f9083a21d674a314721ef06f82db281cbcb052ff8ec1
.0-a82cb2897a8bf9445d68dcc2be05af89ad4b2fda1fddb2952693be7cd5353ad3
.04
.0m1
.0ot
.0qa
.0qg
.14w
.1
.1e5
.1mc
.2pf
.2sa
.2yu
.2zc
.32_13402350686004841
.32_13408249610041376
.32_13408298767158414
.32_13410268068806609
.32_13410701584334782
.32_13410774908705768
.32_13410808930593498
.32_13410808930746352
.32_13410858722815258
.3dk
.3ih
.3jy
.3oy
.3vl
.42c
.4_13410639177679524
.4_13410808930745843
.4_13410839634851478
.4_13410847262681627
.4_13410858722918349
.4_13410860530526431
.4_13410860531260085
.4_13410860531265363
.4_13410860531274139
.4_13410860531280041
.4_13410860531285410
.4_13410862305773690
.4_13410862306462331
.4_13410862306467480
.4_13410862306475723
.4_13410862306481236
.4aj
.4ry
.4wx
.4xy
.4zp
.5
.5r5
.78
.92c
.a
.a4k
.a5a
.adobefeatureflagnotification
.adobestatusnotification
.agents
.ajz
.android
.apache
.aq0
.aqc
.ar1
.asp
.astro
.asy
.autom
.awk
.b3n
.bad
.baf
.baj
.bak-2
.bak legado (v1)
.bak-20260501-201535
.bak-20260626-012214
.bak_2
.bak_20260626_115656
.bak_20260626_170337_rustlog
.bash
.bash_
.bash_logout
.bashrc
.bat
.bazel
.bazelignore
.bazelrc
.bazelversion
.bb3
.bdic
.before
.bg
.bh5
.binarypb
.bin
.blf
.blob
.bpf
.bsd
.build
.bundle
.bzl
.c
.c0b
.c4
.cab
.cache
.cacheinputsfingerprint
.cc
.cdp
.cdpresource
.cfg
.chm
.cjs
.cmd
.codex-backup-wslprompt-20260529
.com_hrd
.com_hrd_metadata
.com_identity_provider
.conf
.conf activo
.conf previo
.config
.csh
.css
.ctm
.cur
.data
.db-shm
.db-wal
.dead
.desktop
.desktop (v1)
.directory
.directory__dd41da77de37
.dist
.dll
.dmp
.dot
.drm
.edb
.etl
.etlgz
.exception
.example
.exe
.f
.fish
.gitattributes
.gitignore
.gitignore__9e3a60f1e6ec
.gitignore__c7db9145bde8
.gyi
.h
.hbc
.igpi
.idx
.ini
.installstate
.ipclog
.ipynb
.iss
.jar
.java
.jcp
.jfm
.jq
.jrs
.json previo
.jsonlz4
.jsp
.jtx
.keystore
.lastupdatedate
.ldb
.lesser
.lib
.license
.list
.lm
.lnk
.lock
.log
.log1
.log2
.loggz
.map
.marker
.md5
.miplog
.mjs
.mpack
.msf
.msi
.nanorc
.node
.npy
.odl
.odlgz
.odlsent
.old
.onnx
.otc
.otc-shm
.otc-wal
.otf
.pack
.pak
.pb
.personality_migration
.php
.pid
.pkl
.pl
.plan
.pm
.post
.profile
.promisor
.proto
.ps1
.ps1xml
.psd1
.psm1
.pth
.py
.pyc
.pyi
.reg
.regtrans-ms
.rels
.repair
.res
.resjson
.resmoncfg
.rev
.s
.sample
.service
.sh
.sha256
.sig
.sqlite-shm
.sqlite-wal
.sqlite3-shm
.sqlite3-wal
.sst
.store
.targets
.test
.tflite
.timer
.tmp
.toml
.ts
.tsx
.ttf
.tz
.uca
.updateuri
.usage
.uuid
.vcrd
.vol
.vpol
.vsch
.vssettings
.vstdir
.vstemplate
.vstman
.wasm
.webmanifest
.whl
.wim
.wmdb
.woff
.woff2
.wxs
.yaml
.yml
"""


REDLIST_ENTRIES = tuple(
    dict.fromkeys(line.strip() for line in _REDLIST_TEXT.splitlines() if line.strip())
)
_REDLIST_CASEFOLDED = frozenset(item.casefold() for item in REDLIST_ENTRIES)


def redlist_policy_payload() -> dict[str, object]:
    """Return the bounded, auditable policy payload."""

    return {
        "schema": REDLIST_POLICY_SCHEMA,
    "match": "basename_exact_or_final_suffix_casefold_v2",
        "entries": list(REDLIST_ENTRIES),
    }


def redlist_policy_digest() -> str:
    encoded = json.dumps(
        redlist_policy_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def redlist_match(path: str | Path) -> str | None:
    """Return the exact configured token matching ``path`` or ``None``.

    Matching is deterministic and case-insensitive.  A token matches either
    the complete basename (for dotfiles and unusual generated names) or the
    final suffix returned by :attr:`Path.suffix`.  Intermediate dotted version
    components (for example ``.1`` in ``0.1-report.xlsx``) are not extensions;
    otherwise short redlist tokens would incorrectly catch ordinary PDF,
    image, spreadsheet, and text files.  No file content or external classifier is
    consulted.
    """

    name = Path(path).name
    folded_name = name.casefold()
    final_suffix = Path(name).suffix.casefold()
    candidates = (folded_name, final_suffix) if final_suffix else (folded_name,)
    for candidate in candidates:
        if candidate in _REDLIST_CASEFOLDED:
            # Return the canonical spelling from the supplied policy, not the
            # filesystem's presentation.
            for entry in REDLIST_ENTRIES:
                if entry.casefold() == candidate:
                    return entry
    return None


__all__ = [
    "REDLIST_ENTRIES",
    "REDLIST_POLICY_SCHEMA",
    "redlist_match",
    "redlist_policy_digest",
    "redlist_policy_payload",
]
