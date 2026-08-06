#!/usr/bin/env python3
# Diamond Trader - Shadow trade e-mail demper updater v1.0

import argparse
import ast
import datetime as dt
import py_compile
import shutil
from pathlib import Path

TARGET = Path("agent.py")
FUNC_NAME = "handle_shadow_trade_notifications"
MARKER = "MAILFLOOD_GUARD_V1"

REPLACEMENT = '''def handle_shadow_trade_notifications(
    agent_state: Dict[str, Any],
) -> int:
    """
    Shadow trade OPEN/CLOSE e-mails zijn bewust gedempt.

    De shadow trades, scanner en Strategy Lab blijven ongewijzigd draaien.
    Nieuwe open/sluit-gebeurtenissen worden wel stil als gezien opgeslagen,
    zodat opnieuw inschakelen later geen oude e-mailachterstand veroorzaakt.

    MAILFLOOD_GUARD_V1
    """
    open_history = set(
        str(value)
        for value in (
            agent_state.get("notified_shadow_open_keys")
            or []
        )
    )

    close_history = set(
        str(value)
        for value in (
            agent_state.get("notified_shadow_close_keys")
            or []
        )
    )

    agent_state.setdefault("notified_shadow_open_keys", [])
    agent_state.setdefault("notified_shadow_close_keys", [])

    new_open_seen = 0
    new_close_seen = 0

    for trade in load_shadow_closed_trades()[-50:]:
        event_key = shadow_event_key("close", trade)
        if event_key in close_history:
            continue
        agent_state["notified_shadow_close_keys"].append(event_key)
        close_history.add(event_key)
        new_close_seen += 1

    for position in load_shadow_open_positions():
        event_key = shadow_event_key("open", position)
        if event_key in open_history:
            continue
        agent_state["notified_shadow_open_keys"].append(event_key)
        open_history.add(event_key)
        new_open_seen += 1

    if new_open_seen or new_close_seen:
        trim_shadow_notification_history(agent_state)
        save_agent_state(agent_state)
        LOG.info(
            "Shadow trade e-mailmeldingen gedempt | "
            "open stil verwerkt=%d | close stil verwerkt=%d",
            new_open_seen,
            new_close_seen,
        )

    return 0
'''

def find_function(source: str):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == FUNC_NAME:
            if node.end_lineno is None:
                raise RuntimeError("Geen end_lineno beschikbaar.")
            return node
    raise RuntimeError(f"Functie {FUNC_NAME!r} niet gevonden.")

def inspect_target(source: str):
    node = find_function(source)
    segment = "\n".join(source.splitlines()[node.lineno - 1: node.end_lineno])
    return node, segment.count("send_email("), MARKER in segment

def apply_patch(source: str) -> str:
    node = find_function(source)
    lines = source.splitlines(keepends=True)
    replacement = REPLACEMENT if REPLACEMENT.endswith("\n") else REPLACEMENT + "\n"
    result = "".join(lines[:node.lineno - 1] + [replacement] + lines[node.end_lineno:])
    ast.parse(result)
    return result

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not TARGET.exists():
        print(f"[FOUT] {TARGET} niet gevonden.")
        return 1

    source = TARGET.read_text(encoding="utf-8")
    try:
        node, send_count, already = inspect_target(source)
    except Exception as exc:
        print(f"[FOUT] Controle mislukt: {exc}")
        return 1

    print("=== DIAMOND SHADOW MAIL DEMPING ===")
    print(f"Bestand              : {TARGET}")
    print(f"Functie               : {FUNC_NAME}")
    print(f"Regels huidige functie: {node.lineno}-{node.end_lineno}")
    print(f"send_email in functie : {send_count}")
    print(f"Patch al aanwezig     : {'JA' if already else 'NEE'}")

    if args.check:
        if already:
            print("[OK] Shadow trade OPEN/CLOSE e-mails zijn al gedempt.")
            return 0
        if send_count != 2:
            print(f"[LET OP] Verwacht 2 send_email-aanroepen, gevonden: {send_count}.")
            return 2
        print("[OK] Doel herkend. Alleen deze functie zal worden vervangen.")
        print("[OK] Overige agent-e-mails blijven onaangeraakt.")
        print("[OK] Geen strategie-, order-, scanner- of configwijziging.")
        return 0

    if already:
        print("[OK] Niets gewijzigd; patch is al aanwezig.")
        return 0

    if send_count != 2:
        print(f"[FOUT] Verwacht exact 2 send_email-aanroepen, gevonden: {send_count}.")
        return 2

    patched = apply_patch(source)

    backup_dir = Path("/var/data/diamond_code_backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"agent.py.before_shadow_mail_mute_{stamp}.bak"
    shutil.copy2(TARGET, backup)

    TARGET.write_text(patched, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except Exception as exc:
        shutil.copy2(backup, TARGET)
        print(f"[FOUT] Syntaxcontrole mislukte; backup teruggezet: {exc}")
        return 1

    after = TARGET.read_text(encoding="utf-8")
    _, after_send_count, after_already = inspect_target(after)
    if not after_already or after_send_count != 0:
        shutil.copy2(backup, TARGET)
        print("[FOUT] Nacontrole mislukte; backup teruggezet.")
        return 1

    print(f"[OK] Backup            : {backup}")
    print("[OK] agent.py syntax   : geldig")
    print("[OK] Shadow OPEN-mail  : uit")
    print("[OK] Shadow CLOSE-mail : uit")
    print("[OK] Oude mail-backlog : wordt voorkomen")
    print("[OK] Overige e-mails   : onaangeraakt")
    print("[OK] Strategie/orders  : onaangeraakt")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
