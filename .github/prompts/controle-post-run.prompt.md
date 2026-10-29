---
mode: agent
description: Contrôle post-run en lecture seule
---
Lancer `sfdata verify --source <src> --target <cible> --json`. Expliquer chaque ligne `ok: false` (écart = non chargés ou apparus
depuis le run : un `delta` les charge ; doublons = identifiant externe à vérifier). Ne rien écrire.
