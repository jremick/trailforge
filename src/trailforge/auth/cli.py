"""Admin & invite management CLI (`python -m trailforge.auth.cli`)."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trailforge.auth.invites import classify_invite_status, create_invite_code
from trailforge.auth.security import hash_password, normalize_email
from trailforge.db.models import InviteCode, User
from trailforge.db.session import get_engine


def _iso_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for ``python -m trailforge.auth.cli``."""
    parser = argparse.ArgumentParser(
        prog="python -m trailforge.auth.cli",
        description="Manage TrailForge admin accounts and friend invite codes.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create-admin", help="Create a new active admin account.")
    p_create.add_argument("--email", required=True, help="Admin account email address.")
    p_create.add_argument(
        "--name",
        "--display-name",
        dest="name",
        required=True,
        help="Admin display name.",
    )
    p_create.add_argument(
        "--password",
        default=None,
        help="Password (at least 8 characters; prefer --password-stdin or interactive prompt).",
    )
    p_create.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read admin password from stdin (avoids exposing secrets in process arguments).",
    )
    p_create.add_argument("--json", action="store_true", help="Output JSON.")

    p_promote = sub.add_parser("promote", help="Promote an existing user to admin.")
    p_promote.add_argument("--email", required=True, help="Existing user email address.")
    p_promote.add_argument("--json", action="store_true", help="Output JSON.")

    p_invite = sub.add_parser("create-invite", help="Create a new invite code (TF-XXXX-XXXX).")
    p_invite.add_argument("--label", default=None, help="Optional label (e.g. friend group).")
    p_invite.add_argument(
        "--role",
        choices=("member", "admin"),
        default="member",
        help="Role granted by this invite code (default: member).",
    )
    p_invite.add_argument(
        "--max-uses",
        type=int,
        default=1,
        help="Maximum number of accounts that can redeem this invite (default: 1).",
    )
    p_invite.add_argument(
        "--expires-days",
        "--expires-in-days",
        dest="expires_days",
        type=int,
        default=None,
        help="Optional expiry duration in days.",
    )
    p_invite.add_argument("--json", action="store_true", help="Output JSON.")

    p_list = sub.add_parser("list-invites", help="List all invite codes and their status.")
    p_list.add_argument("--json", action="store_true", help="Output JSON.")

    return parser


def _resolve_cli_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        raw = sys.stdin.readline().rstrip("\r\n")
        if not raw:
            raise ValueError("No password provided on stdin")
        return raw
    if args.password is not None:
        return str(args.password)
    if sys.stdin.isatty():
        entered = getpass.getpass("Admin password (min 8 chars): ")
        confirm = getpass.getpass("Confirm admin password: ")
        if entered != confirm:
            raise ValueError("Passwords do not match")
        return entered
    raise ValueError("Provide --password-stdin (recommended) or --password")


def _cmd_create_admin(db: Session, args: argparse.Namespace) -> tuple[int, dict[str, Any] | str]:
    try:
        email = normalize_email(args.email)
        password = _resolve_cli_password(args)
        pw_hash = hash_password(password)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2, ""

    display_name = args.name.strip()
    if not display_name:
        print("Error: --name must not be blank", file=sys.stderr)
        return 2, ""

    existing = db.execute(
        select(User).where(func.lower(User.email) == email).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        print(
            f"Error: account {email!r} already exists (use `promote --email {email}`)",
            file=sys.stderr,
        )
        return 1, ""

    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=display_name,
        password_hash=pw_hash,
        role="admin",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    payload = {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active,
    }
    if args.json:
        return 0, payload
    return 0, f"Created admin user {user.email} ({user.display_name}) id={user.id}"


def _cmd_promote(db: Session, args: argparse.Namespace) -> tuple[int, dict[str, Any] | str]:
    try:
        email = normalize_email(args.email)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2, ""

    user = db.execute(
        select(User).where(func.lower(User.email) == email).limit(1)
    ).scalar_one_or_none()
    if user is None:
        print(f"Error: user {email!r} not found", file=sys.stderr)
        return 1, ""

    user.role = "admin"
    user.is_active = True
    user.disabled_at = None
    user.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)

    payload = {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active,
    }
    if args.json:
        return 0, payload
    return 0, f"Promoted {user.email} to admin (active=True)"


def _cmd_create_invite(db: Session, args: argparse.Namespace) -> tuple[int, dict[str, Any] | str]:
    try:
        invite = create_invite_code(
            db,
            label=args.label,
            role=args.role,
            max_uses=args.max_uses,
            expires_in_days=args.expires_days,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2, ""

    db.commit()
    db.refresh(invite)

    payload = {
        "id": str(invite.id),
        "code": invite.code,
        "label": invite.label,
        "role": invite.role,
        "max_uses": invite.max_uses,
        "use_count": invite.use_count,
        "status": classify_invite_status(invite),
        "expires_at": _iso_or_none(invite.expires_at),
        "created_at": _iso_or_none(invite.created_at),
    }
    if args.json:
        return 0, payload
    exp_part = f" expires={payload['expires_at']}" if payload["expires_at"] else " expires=never"
    label_part = f" label={invite.label!r}" if invite.label else ""
    return (
        0,
        f"{invite.code} (role={invite.role} max_uses={invite.max_uses}{exp_part}{label_part})",
    )


def _cmd_list_invites(db: Session, args: argparse.Namespace) -> tuple[int, dict[str, Any] | str]:
    now = datetime.now(UTC)
    invites = (
        db.execute(select(InviteCode).order_by(InviteCode.created_at.desc())).scalars().all()
    )
    items = [
        {
            "id": str(inv.id),
            "code": inv.code,
            "label": inv.label,
            "role": inv.role,
            "max_uses": inv.max_uses,
            "use_count": inv.use_count,
            "status": classify_invite_status(inv, now=now),
            "expires_at": _iso_or_none(inv.expires_at),
            "revoked_at": _iso_or_none(inv.revoked_at),
            "created_at": _iso_or_none(inv.created_at),
        }
        for inv in invites
    ]
    if args.json:
        return 0, {"invites": items, "total": len(items)}
    if not items:
        return 0, "No invite codes found."
    lines = [
        f"{item['code']}  status={item['status']:9s}  role={item['role']:6s}  "
        f"uses={item['use_count']}/{item['max_uses']}  "
        f"expires={item['expires_at'] or 'never'}  label={item['label'] or '-'}"
        for item in items
    ]
    return 0, "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, session: Session | None = None) -> int:
    """CLI entrypoint for admin and invite code operations."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    handlers = {
        "create-admin": _cmd_create_admin,
        "promote": _cmd_promote,
        "create-invite": _cmd_create_invite,
        "list-invites": _cmd_list_invites,
    }
    handler = handlers[args.command]

    if session is not None:
        code, output = handler(session, args)
    else:
        engine = get_engine()
        with Session(engine) as db:
            code, output = handler(db, args)

    if output:
        if isinstance(output, dict):
            print(json.dumps(output, indent=2))
        else:
            print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
