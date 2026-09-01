# Principle: keeper-of-locus (settled with the operator 2026-08-21)

1. **Every locus runs a keeper, and that keeper owns that locus** — op's keeper owns op;
   ae's keeper owns ae (lxd, GPUs, the share); each VM's keeper its VM; a worker's keeper its GPU.
   "Host" is not a privilege; it is the locus that happens to own the hypervisor.
2. **The station is the medium, and the keeper may revise the medium it is in.** Station files
   on a locus are keeper-writable (group `station-keepers`); every revision is a receipt in
   `~/.hugpy/station-revisions/revisions.jsonl` with a backup and a rollback (`ops/station-revise.sh`),
   and is reconciled into the Station source before the next release.
3. **Coordination is peer-to-peer.** Boards (`~/todo.json`), canvas (`~/flow.json`), mail
   (`~/keeper-mail/`, kmsg.v1, `ops/keeper-msg.sh`), and `~/keeper-mail/{peers,locus}.json` are the
   fabric. Any keeper may message, request, and propose to any other. Ids are namespaced per
   keeper (`or-*` hugpy, `opk-*` op).
4. **What does not self-regulate stays with the operator:** rights/consent, credentials,
   licences/accounts, and product decisions (proposal cards). These are about the world outside
   the fleet, not the fleet — the only authority gates a host is "held to".
5. **Honesty rules carry over:** verify-after-write, receipts for everything shipped, never mark
   done without evidence, never quote redacted content.

First instance: op (2026-08-21) — group grant, revision log, locus.json; hugpy VM as peer.
Next loci: ae (needs its own keeper seat + sudoers line), computron, a-brain-Super-Server.
