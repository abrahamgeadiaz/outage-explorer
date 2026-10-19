"""Seeded test users, one per persona (the passwords are listed in the README).

Only scrypt hashes are stored here (auth.hash_password). A real deployment would keep users in a
database or delegate login to an identity provider (OIDC); the rest of the API only needs a username
and a role, so swapping this module out does not touch authorization.
"""

from dataclasses import dataclass

from outage_explorer.api.policy import Role


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    role: Role
    password_hash: str


USERS: dict[str, User] = {
    u.username: u
    for u in (
        User(
            "analyst",
            "Ana Analyst",
            Role.ANALYST,
            "scrypt$eAsqucUZ9h33yUcFTHwHYQ==$vZDz6Rb9hNgTJceXcrAjhe82QDortxfUTmnkjBatn5g=",
        ),
        User(
            "viewer",
            "Vic Viewer",
            Role.VIEWER,
            "scrypt$KYLVATrZHjgJfIgg9JW4OQ==$2Rsa90a5ZjBAa--lqCLB7eJDRdh2P1TrG2GGFbntS2c=",
        ),
        User(
            "admin",
            "Ada Admin",
            Role.ADMIN,
            "scrypt$ywQ1Qf9DLbnVdCKKEGG39A==$E80sXvqotaismKoRSJo0tn1AR9fyRuahF9h_tDHBUMk=",
        ),
    )
}
