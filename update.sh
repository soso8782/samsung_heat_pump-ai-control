#!/bin/bash
# Bezpečná aktualizácia inštalácie agenta kúrenia z gitu: len fast-forward, kontrola Python syntaxe,
# reštart služieb a kontrola zdravia; pri zlyhaní sa vráti na predchádzajúci commit.
# Lokálne súbory (config.env, *.db, profil.json, cf_token, backup/ ...) nie sú sledované gitom, takže ich nemení.
# Použitie: update.sh [--dry-run] [--ref <ref>] [adresár]
#   --dry-run    len ukáže, čo by sa zmenilo (fetch, log, diff --stat); nič nemení
#   --ref <ref>  cieľ aktualizácie, napr. origin/main alebo tag v2.12.0 (predvolene upstream aktuálnej vetvy)
#   adresár      inštalácia (predvolene $KURENIE_DIR alebo /opt/kurenie)
set -euo pipefail
export GIT_PAGER=cat

SERVICES=(kurenie-collector kurenie-monitor kurenie-web kurenie-agent)
HTTP_OK='
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen("http://127.0.0.1/", timeout=5)
except urllib.error.HTTPError as e:
    sys.exit(0 if e.code < 500 else 1)
except Exception:
    sys.exit(1)
'

usage() {
    echo "Použitie: update.sh [--dry-run] [--ref <ref>] [adresár]"
    echo "  --dry-run    len ukáže, čo by sa zmenilo; nič nemení"
    echo "  --ref <ref>  cieľ aktualizácie (napr. origin/main, v2.12.0); predvolene upstream vetvy"
    echo "  adresár      inštalácia; predvolene \$KURENIE_DIR alebo /opt/kurenie"
}

DRY=0
REF=""
K=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --ref)
            if [ $# -lt 2 ]; then echo "CHYBA: --ref vyžaduje hodnotu." >&2; exit 2; fi
            REF="$2"; shift 2 ;;
        --ref=*) REF="${1#--ref=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "CHYBA: neznáma voľba $1" >&2; usage >&2; exit 2 ;;
        *) K="$1"; shift ;;
    esac
done
K="${K:-${KURENIE_DIR:-/opt/kurenie}}"

cd "$K" 2>/dev/null || { echo "CHYBA: adresár $K neexistuje." >&2; exit 1; }
TOP=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "CHYBA: $K nie je git repozitár." >&2; exit 1; }
if [ "$TOP" != "$(pwd -P)" ]; then
    echo "CHYBA: $K nie je koreň git repozitára ($TOP)." >&2
    exit 1
fi

# Zmenené sledované súbory by sa pri aktualizácii alebo návrate prepísali, preto ich nepustíme ďalej.
DIRTY=$(git status --porcelain --untracked-files=no)
if [ -n "$DIRTY" ]; then
    echo "CHYBA: v $K sú zmenené sledované súbory (aktualizácia by ich prepísala):" >&2
    echo "$DIRTY" >&2
    exit 1
fi

if [ -z "$REF" ]; then
    REF=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || {
        echo "CHYBA: aktuálna vetva nemá upstream; zadaj cieľ cez --ref <ref>." >&2
        exit 1
    }
fi

OLD=$(git rev-parse HEAD)
OLD_VER=$(git show "$OLD:VERSION" 2>/dev/null || echo "?")

echo "Sťahujem zmeny (git fetch)..."
if ! git fetch --tags --quiet; then
    echo "CHYBA: git fetch zlyhal (sieť alebo prístup k repozitáru)." >&2
    exit 1
fi

NEW=$(git rev-parse --verify --quiet "$REF^{commit}") || { echo "CHYBA: ref '$REF' neexistuje." >&2; exit 1; }
NEW_VER=$(git show "$NEW:VERSION" 2>/dev/null || echo "?")

if [ "$NEW" = "$OLD" ]; then
    echo "Už je aktuálne: commit ${OLD:0:7}, verzia $OLD_VER (cieľ $REF)."
    exit 0
fi
if ! git merge-base --is-ancestor "$OLD" "$NEW"; then
    echo "CHYBA: $REF nie je fast-forward od commitu ${OLD:0:7} (lokálne commity, rozdelená história alebo staršia verzia)." >&2
    echo "Zastavené; nič nebolo zmenené." >&2
    exit 1
fi

if [ "$DRY" -eq 1 ]; then
    echo "=== Náhľad (--dry-run), nič sa nemení ==="
    echo "Verzia: $OLD_VER → $NEW_VER (commit ${OLD:0:7} → ${NEW:0:7})"
    echo "Nové commity:"
    git log --oneline "$OLD..$NEW"
    echo "Zmeny:"
    git diff --stat "$OLD" "$NEW"
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "CHYBA: aktualizácia musí bežať ako root (služby systemd)." >&2
    exit 1
fi

stav_sluzieb() {  # vráti 1, ak niektorá služba nebeží
    local s rc=0
    for s in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$s"; then
            echo "  OK   $s"
        else
            echo "  ZLE  $s"
            rc=1
        fi
    done
    return $rc
}

# Návrat na predchádzajúci commit. $1 = restart (služby už boli reštartované novou verziou).
vrat() {
    echo "Vraciam sa na commit ${OLD:0:7} (verzia $OLD_VER)..." >&2
    if ! git reset --hard --quiet "$OLD"; then
        echo "KRITICKÉ: návrat zlyhal; ručne: cd $K && git reset --hard $OLD" >&2
        exit 1
    fi
    if [ "${1:-}" = restart ]; then
        echo "Reštartujem služby so starou verziou..." >&2
        systemctl restart "${SERVICES[@]}" || echo "POZOR: reštart služieb po návrate zlyhal, skontroluj ručne." >&2
        sleep 5
        stav_sluzieb >&2 || true
    fi
    echo "Aktualizácia neúspešná; inštalácia je späť na ${OLD:0:7}." >&2
    exit 1
}

echo "Aktualizujem z $OLD_VER (${OLD:0:7}) na $NEW_VER (${NEW:0:7})..."
if ! git merge --ff-only --quiet "$NEW"; then
    echo "CHYBA: git merge --ff-only zlyhal (pozri hlásenie vyššie)." >&2
    exit 1
fi

echo "Kontrolujem syntax Python súborov (py_compile)..."
mapfile -d '' -t PY < <(git ls-files -z -- '*.py')
if [ "${#PY[@]}" -gt 0 ] && ! python3 -m py_compile "${PY[@]}"; then
    echo "CHYBA: syntaktická chyba v novej verzii." >&2
    vrat
fi

echo "Reštartujem služby: ${SERVICES[*]}"
if ! systemctl restart "${SERVICES[@]}"; then
    echo "CHYBA: reštart služieb zlyhal." >&2
    vrat restart
fi

echo "Čakám 5 s a kontrolujem služby a http://127.0.0.1/ ..."
sleep 5
PROBLEMY=0
stav_sluzieb || PROBLEMY=1
if python3 -c "$HTTP_OK"; then
    echo "  OK   http://127.0.0.1/"
else
    echo "  ZLE  http://127.0.0.1/ neodpovedá"
    PROBLEMY=1
fi
if [ "$PROBLEMY" -ne 0 ]; then
    echo "CHYBA: nová verzia nenaštartovala správne." >&2
    vrat restart
fi

echo "Hotovo: verzia $OLD_VER → $NEW_VER (commit ${OLD:0:7} → ${NEW:0:7})."
echo "Zmeny:"
git log --oneline "$OLD..$NEW"
