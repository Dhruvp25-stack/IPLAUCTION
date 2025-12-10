import csv
import os
import random
import re
from dataclasses import dataclass
from typing import List, Optional, Dict

import pdfplumber
from flask import (
    Flask, request, redirect, url_for,
    render_template_string, session, flash
)

# ----------------- CONFIG -----------------

SECRET_KEY = "change-this-secret"  # change for real use
ADMIN_PASSWORD = "ipladmin"        # change for real use

PLAYERS_CSV = "players.csv"
PLAYERS_PDF = "players.pdf"

# Default IPL-style teams and purses (in crore)
DEFAULT_TEAMS = [
    ("CSK", 100),
    ("MI", 100),
    ("RCB", 100),
    ("KKR", 100),
    ("SRH", 100),
    ("RR", 100),
    ("DC", 100),
    ("PBKS", 100),
    ("GT", 100),
    ("LSG", 100),
]


# ----------------- DATA MODELS -----------------

@dataclass
class Player:
    id: int
    set_no: int
    set_code: str
    first_name: str
    surname: str
    country: str
    base_price: float  # in lakh
    role: str = ""     # e.g. BATTER/ALL-ROUNDER (optional)

    @property
    def full_name(self):
        return f"{self.first_name} {self.surname}".strip()


@dataclass
class Team:
    name: str
    purse_total: float  # in crore
    purse_remaining: float  # in crore
    taken_by: Optional[str] = None  # display name
    squad: Optional[List[int]] = None  # list of player ids

    def to_dict(self):
        return {
            "name": self.name,
            "purse_total": self.purse_total,
            "purse_remaining": self.purse_remaining,
            "taken_by": self.taken_by,
            "squad": self.squad or [],
        }


@dataclass
class Bid:
    player_id: int
    team_name: str
    amount: float  # in crore


# ----------------- APP STATE -----------------

app = Flask(__name__)
app.secret_key = SECRET_KEY

PLAYERS: Dict[int, Player] = {}
TEAMS: Dict[str, Team] = {}

AUCTION_STARTED: bool = False
AUCTION_ORDER: List[int] = []  # list of player ids
CURRENT_INDEX: int = -1        # index in AUCTION_ORDER
CURRENT_BID: Optional[Bid] = None
SOLD_PLAYERS: Dict[int, Dict] = {}  # player_id -> info


# ----------------- PLAYER LOADERS -----------------

def reset_auction_state():
    global AUCTION_STARTED, AUCTION_ORDER, CURRENT_INDEX, CURRENT_BID, SOLD_PLAYERS
    AUCTION_STARTED = False
    AUCTION_ORDER = []
    CURRENT_INDEX = -1
    CURRENT_BID = None
    SOLD_PLAYERS = {}


def load_players_from_csv(path: str):
    """
    Load players from a CSV file with headers like:
    Set No.,2026 Set,First Name,Surname,Country,Reserve Price Rs Lakh,Specialism
    """
    global PLAYERS
    PLAYERS = {}

    if not os.path.exists(path):
        return

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        pid = 1
        for row in reader:
            try:
                set_no = int(row.get("Set No.", "0") or 0)
            except ValueError:
                continue

            set_code = (row.get("2026 Set") or row.get("Set Code") or "").strip()
            first_name = (row.get("First Name") or row.get("Player") or "").strip()
            surname = (row.get("Surname") or row.get("Last Name") or "").strip()
            country = (row.get("Country") or "").strip()
            role = (row.get("Specialism") or row.get("Role") or "").strip()

            base_raw = (row.get("Reserve Price Rs Lakh") or row.get("Base Price") or "0").strip()

            # Allow things like "200", "INR 200", "200.0", etc.
            digits = re.sub(r"[^0-9.]", "", base_raw)
            try:
                base_price = float(digits) if digits else 0.0
            except ValueError:
                base_price = 0.0

            if not first_name and not surname:
                continue

            PLAYERS[pid] = Player(
                id=pid,
                set_no=set_no,
                set_code=set_code,
                first_name=first_name,
                surname=surname,
                country=country,
                base_price=base_price,
                role=role,
            )
            pid += 1

    reset_auction_state()


def load_players_from_pdf(path: str):
    """
    Read IPL Auction PDF and fill PLAYERS dict by extracting tables.
    It tries to auto-detect column indexes by header names.
    """
    global PLAYERS
    PLAYERS = {}

    if not os.path.exists(path):
        return

    pid = 1
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue

                headers = [(h or "").strip() for h in table[0]]
                if not any(headers):
                    continue

                # helper to find column by partial header names
                def find_col(name_options: List[str]) -> Optional[int]:
                    for idx, header in enumerate(headers):
                        h_low = header.lower()
                        for n in name_options:
                            if n in h_low:
                                return idx
                    return None

                idx_set_no = find_col(["set no"])
                idx_set_code = find_col(["2026 set", "set code", "set"])
                idx_first = find_col(["first name", "player"])
                idx_surname = find_col(["surname", "last name"])
                idx_country = find_col(["country"])
                idx_price = find_col(["reserve price", "base price"])
                idx_role = find_col(["specialism", "role", "playing role"])

                for row in table[1:]:
                    # ensure length
                    row = list(row) + [""] * (len(headers) - len(row))

                    def get(idx: Optional[int]) -> str:
                        if idx is None or idx >= len(row):
                            return ""
                        cell = row[idx]
                        return str(cell).strip() if cell is not None else ""

                    try:
                        set_no = int(get(idx_set_no) or 0)
                    except ValueError:
                        set_no = 0

                    set_code = get(idx_set_code)
                    first_name = get(idx_first)
                    surname = get(idx_surname)
                    country = get(idx_country)
                    role = get(idx_role)

                    price_raw = get(idx_price)
                    digits = re.sub(r"[^0-9.]", "", price_raw)
                    try:
                        base_price = float(digits) if digits else 0.0
                    except ValueError:
                        base_price = 0.0

                    if not first_name and not surname:
                        continue

                    PLAYERS[pid] = Player(
                        id=pid,
                        set_no=set_no,
                        set_code=set_code,
                        first_name=first_name,
                        surname=surname,
                        country=country,
                        base_price=base_price,
                        role=role,
                    )
                    pid += 1

    reset_auction_state()


def load_players_auto():
    """
    Load players from last saved file if PLAYERS is empty.
    Prefers PDF, then CSV.
    """
    if PLAYERS:
        return
    if os.path.exists(PLAYERS_PDF):
        load_players_from_pdf(PLAYERS_PDF)
    elif os.path.exists(PLAYERS_CSV):
        load_players_from_csv(PLAYERS_CSV)


# ----------------- OTHER UTILS -----------------

def init_teams():
    global TEAMS
    if TEAMS:
        return
    for name, purse in DEFAULT_TEAMS:
        TEAMS[name] = Team(
            name=name,
            purse_total=purse,
            purse_remaining=purse,
            taken_by=None,
            squad=[]
        )


def is_admin():
    return session.get("is_admin", False)


def current_team():
    team_name = session.get("team_name")
    if team_name and team_name in TEAMS:
        return TEAMS[team_name]
    return None


def build_auction_order():
    """Sets in order, players within set shuffled."""
    global AUCTION_ORDER, CURRENT_INDEX, CURRENT_BID, AUCTION_STARTED
    AUCTION_ORDER = []
    if not PLAYERS:
        return
    # group by set_no
    sets: Dict[int, List[Player]] = {}
    for p in PLAYERS.values():
        sets.setdefault(p.set_no, []).append(p)

    for s in sorted(sets.keys()):
        players = sets[s]
        random.shuffle(players)
        AUCTION_ORDER.extend([p.id for p in players])

    AUCTION_STARTED = True
    CURRENT_INDEX = 0
    CURRENT_BID = None


def get_current_player() -> Optional[Player]:
    if not AUCTION_STARTED or CURRENT_INDEX < 0 or CURRENT_INDEX >= len(AUCTION_ORDER):
        return None
    pid = AUCTION_ORDER[CURRENT_INDEX]
    return PLAYERS.get(pid)


# ----------------- TEMPLATES -----------------

BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{{ title or "IPL Mock Auction" }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background:#050816; color:#f9fafb;}
    a { color:#38bdf8; text-decoration:none; }
    a:hover { text-decoration:underline; }
    .container { max-width: 1100px; margin: 0 auto; padding: 1.5rem; }
    header { background:#020617; border-bottom:1px solid #111827; padding:1rem 0; }
    .brand { font-size:1.5rem; font-weight:700; color:#22c55e; }
    .card { background:#020617; border-radius:1rem; padding:1.5rem; margin-bottom:1rem; box-shadow:0 20px 40px rgba(0,0,0,0.4); border:1px solid #111827;}
    .btn { padding:0.5rem 1rem; border-radius:999px; border:none; cursor:pointer; font-weight:600; }
    .btn-primary{ background:#22c55e; color:#020617; }
    .btn-primary:hover{ background:#16a34a; }
    .btn-danger{ background:#ef4444; color:white;}
    .btn-secondary{ background:#111827; color:#e5e7eb; border:1px solid #1f2937;}
    .badge{ display:inline-block; padding:0.25rem 0.6rem; border-radius:999px; font-size:0.75rem; background:#111827; color:#e5e7eb; margin-right:0.25rem;}
    .grid{ display:grid; gap:1rem;}
    .grid-2{ grid-template-columns: repeat(auto-fit,minmax(260px,1fr));}
    .muted{ color:#9ca3af; font-size:0.85rem;}
    input, select { background:#020617; border:1px solid #1f2937; border-radius:0.75rem; padding:0.5rem 0.75rem; color:#e5e7eb; width:100%;}
    label{ font-size:0.85rem; color:#9ca3af; display:block; margin-bottom:0.25rem;}
    table{ width:100%; border-collapse:collapse; font-size:0.9rem;}
    th,td{ padding:0.4rem 0.6rem; border-bottom:1px solid #111827; text-align:left;}
    th{ color:#9ca3af; font-weight:500;}
    .tag-green{ color:#22c55e;}
    .flash{ padding:0.5rem 0.75rem; border-radius:0.75rem; margin-bottom:0.75rem; background:#0f172a; }
  </style>
</head>
<body>
<header>
 <div class="container" style="display:flex;justify-content:space-between;align-items:center;">
   <div class="brand">IPL Mock Auction</div>
   <nav style="font-size:0.9rem;">
     <a href="{{ url_for('home') }}">Home</a>
     {% if session.get('team_name') %}
       <span class="badge">Team: {{ session.get('team_name') }}</span>
       <a href="{{ url_for('unselect_team') }}">Unselect team</a>
     {% endif %}
     {% if session.get('is_admin') %}
       &nbsp;&nbsp;<a href="{{ url_for('admin_dashboard') }}">Admin</a>
     {% else %}
       &nbsp;&nbsp;<a href="{{ url_for('admin_login') }}">Admin Login</a>
     {% endif %}
   </nav>
 </div>
</header>
<div class="container">
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      {% for m in messages %}
        <div class="flash">{{ m }}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}
  {{ body|safe }}
</div>
</body>
</html>
"""


def render_page(title, body):
    return render_template_string(BASE_TEMPLATE, title=title, body=body)


# ----------------- ROUTES -----------------

@app.route("/")
def home():
    init_teams()
    load_players_auto()
    team = current_team()
    body = render_template_string("""
    <div class="card">
      <h2 style="font-size:1.4rem;font-weight:600;margin-bottom:0.5rem;">Welcome to IPL Mock Auction</h2>
      <p class="muted">Choose a team, wait for admin to start the auction, then bid for players in real time.</p>
    </div>

    <div class="grid grid-2">
      <div class="card">
        <h3 style="font-size:1.1rem;margin-bottom:0.75rem;">Your status</h3>
        {% if team %}
          <p>You are playing as <span class="tag-green">{{ team.name }}</span>.</p>
          <p class="muted">Purse remaining: {{ "%.2f"|format(team.purse_remaining) }} Cr</p>
        {% else %}
          <p>You have not selected a team yet.</p>
          <a href="{{ url_for('select_team') }}" class="btn btn-primary">Select team</a>
        {% endif %}
        <p class="muted" style="margin-top:0.75rem;">
          {% if AUCTION_STARTED %}
            Auction status: <span class="tag-green">Live</span>
          {% else %}
            Auction status: Not started
          {% endif %}
        </p>
        <p><a href="{{ url_for('auction_room') }}">Go to Auction Room</a></p>
      </div>

      <div class="card">
        <h3 style="font-size:1.1rem;margin-bottom:0.75rem;">Teams</h3>
        <table>
         <tr><th>Team</th><th>Owner</th><th>Purse left (Cr)</th></tr>
         {% for t in teams.values() %}
           <tr>
             <td>{{ t.name }}</td>
             <td>{{ t.taken_by or "-" }}</td>
             <td>{{ "%.2f"|format(t.purse_remaining) }}</td>
           </tr>
         {% endfor %}
        </table>
      </div>
    </div>
    """, team=team, teams=TEAMS, AUCTION_STARTED=AUCTION_STARTED)
    return render_page("Home", body)


@app.route("/select-team", methods=["GET", "POST"])
def select_team():
    init_teams()
    if AUCTION_STARTED:
        flash("Auction already started. You cannot change teams now.")
        return redirect(url_for("home"))

    if request.method == "POST":
        team_name = request.form.get("team")
        display_name = request.form.get("display_name") or "Player"
        if not team_name or team_name not in TEAMS:
            flash("Invalid team.")
            return redirect(url_for("select_team"))
        team = TEAMS[team_name]
        if team.taken_by:
            flash("That team is already taken.")
            return redirect(url_for("select_team"))
        # free old team if any
        old_team = current_team()
        if old_team:
            old_team.taken_by = None
        team.taken_by = display_name
        session["team_name"] = team_name
        flash(f"You have taken team {team_name}.")
        return redirect(url_for("home"))

    available = [t for t in TEAMS.values() if not t.taken_by]
    body = render_template_string("""
    <div class="card">
      <h2 style="font-size:1.3rem;margin-bottom:0.5rem;">Select your IPL Team</h2>
      {% if not available %}
        <p>All teams are already taken.</p>
      {% else %}
        <form method="post" class="grid grid-2">
          <div>
            <label>Your name / nickname</label>
            <input type="text" name="display_name" placeholder="e.g. Dhruv" required>
          </div>
          <div>
            <label>Team</label>
            <select name="team" required>
              <option value="">-- Select --</option>
              {% for t in available %}
                <option value="{{ t.name }}">{{ t.name }}</option>
              {% endfor %}
            </select>
          </div>
          <div style="grid-column:1/-1;">
            <button class="btn btn-primary" type="submit">Take this team</button>
          </div>
        </form>
      {% endif %}
    </div>
    """, available=available)
    return render_page("Select Team", body)


@app.route("/unselect-team")
def unselect_team():
    init_teams()
    if AUCTION_STARTED:
        flash("Auction already started. You cannot unselect team now.")
        return redirect(url_for("home"))

    team = current_team()
    if team:
        team.taken_by = None
        session.pop("team_name", None)
        flash("You have left your team.")
    else:
        flash("You are not currently assigned to any team.")
    return redirect(url_for("home"))


# ------------- ADMIN --------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password")
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            flash("Admin login successful.")
            return redirect(url_for("admin_dashboard"))
        flash("Wrong password.")
    body = """
    <div class="card">
      <h2 style="font-size:1.3rem;margin-bottom:0.5rem;">Admin Login</h2>
      <form method="post">
        <label>Password</label>
        <input type="password" name="password" required>
        <div style="margin-top:0.75rem;">
          <button type="submit" class="btn btn-primary">Login</button>
        </div>
      </form>
    </div>
    """
    return render_page("Admin Login", body)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out of admin.")
    return redirect(url_for("home"))


@app.route("/admin", methods=["GET", "POST"])
def admin_dashboard():
    init_teams()
    load_players_auto()
    if not is_admin():
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        # update team purses
        for name in TEAMS:
            key = f"purse_{name}"
            if key in request.form:
                try:
                    val = float(request.form[key])
                    TEAMS[name].purse_total = val
                    # adjust remaining only if no auction yet
                    if not AUCTION_STARTED:
                        TEAMS[name].purse_remaining = val
                except ValueError:
                    pass
        flash("Team purses updated.")

    body = render_template_string("""
    <div class="card">
      <h2 style="font-size:1.3rem;margin-bottom:0.5rem;">Admin Dashboard</h2>
      <p class="muted">Upload player list (PDF or CSV), manage team purses and control auction.</p>
      <p>
        Auction status:
        {% if AUCTION_STARTED %}
          <span class="tag-green">Live</span>
        {% else %}
          Not started
        {% endif %}
      </p>
      <form action="{{ url_for('upload_players') }}" method="post" enctype="multipart/form-data" style="margin-top:0.5rem;">
        <label>Upload players file (IPL Auction PDF or CSV)</label>
        <input type="file" name="file" accept=".pdf,.csv" required>
        <button class="btn btn-secondary" type="submit" style="margin-top:0.5rem;">Upload & Reload Players</button>
      </form>
    </div>

    <div class="card">
      <h3 style="font-size:1.1rem;margin-bottom:0.75rem;">Team Owners</h3>
      <table>
        <tr><th>Team</th><th>Owner</th></tr>
        {% for t in teams.values() %}
          <tr>
            <td>{{ t.name }}</td>
            <td>{{ t.taken_by or "-" }}</td>
          </tr>
        {% endfor %}
      </table>
    </div>

    <div class="card">
      <h3 style="font-size:1.1rem;margin-bottom:0.75rem;">Team Purses (Cr)</h3>
      <form method="post">
        <table>
          <tr><th>Team</th><th>Owner</th><th>Total purse</th><th>Purse left</th></tr>
          {% for t in teams.values() %}
            <tr>
              <td>{{ t.name }}</td>
              <td>{{ t.taken_by or "-" }}</td>
              <td>
                <input type="number" step="0.1" name="purse_{{ t.name }}" value="{{ t.purse_total }}">
              </td>
              <td>{{ "%.2f"|format(t.purse_remaining) }}</td>
            </tr>
          {% endfor %}
        </table>
        <button class="btn btn-primary" type="submit" style="margin-top:0.75rem;">Save purses</button>
      </form>
    </div>

    <div class="card">
      <h3 style="font-size:1.1rem;margin-bottom:0.75rem;">Auction Controls</h3>
      {% if not AUCTION_STARTED %}
        <form method="post" action="{{ url_for('start_auction') }}">
          <button class="btn btn-primary" type="submit">Start Auction</button>
        </form>
      {% else %}
        <p>Current index: {{ CURRENT_INDEX + 1 }} / {{ AUCTION_ORDER|length }}</p>
        <p><a href="{{ url_for('auction_room') }}">Go to Auction Room</a></p>
        <form method="post" action="{{ url_for('end_auction') }}" style="margin-top:0.75rem;">
          <button class="btn btn-danger" type="submit">End Auction</button>
        </form>
      {% endif %}
    </div>
    """,
    teams=TEAMS,
    AUCTION_STARTED=AUCTION_STARTED,
    CURRENT_INDEX=CURRENT_INDEX,
    AUCTION_ORDER=AUCTION_ORDER)
    return render_page("Admin", body)


@app.route("/admin/upload", methods=["POST"])
def upload_players():
    if not is_admin():
        return redirect(url_for("admin_login"))
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("No file uploaded.")
        return redirect(url_for("admin_dashboard"))

    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".pdf":
        save_path = PLAYERS_PDF
        file.save(save_path)
        load_players_from_pdf(save_path)
        flash(f"Loaded players from PDF ({filename}).")
    elif ext == ".csv":
        save_path = PLAYERS_CSV
        file.save(save_path)
        load_players_from_csv(save_path)
        flash(f"Loaded players from CSV ({filename}).")
    else:
        flash("Unsupported file type. Please upload a PDF or CSV.")
        return redirect(url_for("admin_dashboard"))

    if not PLAYERS:
        flash("Warning: no players were detected. Please check the file format.")
    else:
        flash(f"{len(PLAYERS)} players loaded.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/start", methods=["POST"])
def start_auction():
    if not is_admin():
        return redirect(url_for("admin_login"))
    if not PLAYERS:
        flash("No players loaded.")
        return redirect(url_for("admin_dashboard"))
    build_auction_order()
    flash("Auction started.")
    return redirect(url_for("auction_room"))


@app.route("/admin/end", methods=["POST"])
def end_auction():
    global AUCTION_STARTED, CURRENT_INDEX, CURRENT_BID
    if not is_admin():
        return redirect(url_for("admin_login"))

    AUCTION_STARTED = False
    CURRENT_INDEX = -1
    CURRENT_BID = None

    flash("Auction has been ended by admin.")
    return redirect(url_for("auction_room"))


# ------------- AUCTION ROOM -------------

@app.route("/auction")
def auction_room():
    init_teams()
    load_players_auto()
    player = get_current_player()
    team = current_team()
    body = render_template_string("""
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h2 style="font-size:1.3rem;">Auction Room</h2>
        {% if AUCTION_STARTED %}
          <span class="badge">Live</span>
        {% else %}
          <span class="badge">Not Live</span>
        {% endif %}
      </div>
      {% if not AUCTION_STARTED %}
        <p class="muted">Auction is not live. Wait for admin to start or it has already ended.</p>
      {% elif not player %}
        <p>All players have been processed.</p>
      {% else %}
        <div class="grid grid-2" style="margin-top:0.75rem;">
          <div>
            <h3 style="font-size:1.1rem;margin-bottom:0.25rem;">Current Player</h3>
            <p style="font-size:1.15rem;font-weight:600;">{{ player.full_name }}</p>
            <p class="muted">{{ player.country }} &middot; {{ player.role or "Player" }}</p>
            <p>Set: {{ player.set_no }} ({{ player.set_code }})</p>
            <p>Base price: <span class="tag-green">{{ "%.2f"|format(player.base_price / 100.0) }} Cr</span> ({{ player.base_price }} Lakh)</p>
          </div>
          <div>
            <h3 style="font-size:1.1rem;margin-bottom:0.25rem;">Current Bid</h3>
            {% if current_bid %}
              <p><strong>{{ current_bid.team_name }}</strong> &mdash; {{ "%.2f"|format(current_bid.amount) }} Cr</p>
            {% else %}
              <p>No bids yet. Start from base price or above.</p>
            {% endif %}
            <h4 style="margin-top:0.75rem;font-size:1rem;">Your Team</h4>
            {% if team %}
              <p class="muted">{{ team.name }} &middot; Purse left: {{ "%.2f"|format(team.purse_remaining) }} Cr</p>
              {% if AUCTION_STARTED %}
                <form method="post" action="{{ url_for('place_bid') }}">
                  <label>Bid amount (Cr)</label>
                  <input type="number" step="0.1" min="0" name="amount" required>
                  <button class="btn btn-primary" type="submit" style="margin-top:0.5rem;">Place Bid</button>
                </form>
              {% else %}
                <p class="muted">You cannot bid right now.</p>
              {% endif %}
            {% else %}
              <p>You must select a team before bidding.</p>
              <a class="btn btn-secondary" href="{{ url_for('select_team') }}">Select team</a>
            {% endif %}
          </div>
        </div>
      {% endif %}
    </div>

    {% if is_admin %}
      <div class="card">
        <h3 style="font-size:1.05rem;margin-bottom:0.5rem;">Admin Controls</h3>
        {% if AUCTION_STARTED and player %}
          <form method="post" action="{{ url_for('sell_player') }}" style="display:inline;">
            <button class="btn btn-primary" type="submit">Sell to Highest Bid</button>
          </form>
          <form method="post" action="{{ url_for('unsold_player') }}" style="display:inline;margin-left:0.5rem;">
            <button class="btn btn-secondary" type="submit">Mark Unsold</button>
          </form>
          <form method="post" action="{{ url_for('next_player') }}" style="display:inline;margin-left:0.5rem;">
            <button class="btn btn-danger" type="submit">Next Player</button>
          </form>
        {% endif %}
        <form method="post" action="{{ url_for('end_auction') }}" style="display:inline;margin-left:0.5rem;margin-top:0.5rem;">
          <button class="btn btn-danger" type="submit">End Auction</button>
        </form>
      </div>
    {% endif %}

    <div class="card">
      <h3 style="font-size:1.05rem;margin-bottom:0.5rem;">Sold Players</h3>
      {% if not sold %}
        <p class="muted">No players sold yet.</p>
      {% else %}
        <table>
          <tr><th>Player</th><th>Team</th><th>Price (Cr)</th></tr>
          {% for s in sold.values() %}
            <tr>
              <td>{{ s.player_name }}</td>
              <td>{{ s.team_name or "-" }}</td>
              <td>{{ "%.2f"|format(s.price_cr) if s.team_name else "-" }}</td>
            </tr>
          {% endfor %}
        </table>
      {% endif %}
    </div>

    """, AUCTION_STARTED=AUCTION_STARTED, player=player,
       team=team, current_bid=CURRENT_BID, sold=SOLD_PLAYERS,
       is_admin=is_admin())
    return render_page("Auction Room", body)


@app.route("/auction/bid", methods=["POST"])
def place_bid():
    global CURRENT_BID
    if not AUCTION_STARTED:
        flash("Auction not started.")
        return redirect(url_for("auction_room"))
    team = current_team()
    if not team:
        flash("Select a team before bidding.")
        return redirect(url_for("auction_room"))
    player = get_current_player()
    if not player:
        flash("No active player.")
        return redirect(url_for("auction_room"))

    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        flash("Invalid amount.")
        return redirect(url_for("auction_room"))

    if amount <= 0:
        flash("Bid must be positive.")
        return redirect(url_for("auction_room"))

    # base price in crore
    min_price = player.base_price / 100.0
    min_allowed = max(min_price, (CURRENT_BID.amount + 0.1) if CURRENT_BID else min_price)
    if amount < min_allowed - 1e-6:
        flash(f"Bid must be at least {min_allowed:.2f} Cr.")
        return redirect(url_for("auction_room"))

    if amount > team.purse_remaining + 1e-6:
        flash("You do not have enough purse.")
        return redirect(url_for("auction_room"))

    CURRENT_BID = Bid(player_id=player.id, team_name=team.name, amount=amount)
    flash(f"Bid placed: {team.name} @ {amount:.2f} Cr")
    return redirect(url_for("auction_room"))


@app.route("/auction/sell", methods=["POST"])
def sell_player():
    global CURRENT_INDEX, CURRENT_BID
    if not is_admin():
        return redirect(url_for("auction_room"))
    if not AUCTION_STARTED:
        flash("Auction is not live.")
        return redirect(url_for("auction_room"))
    player = get_current_player()
    if not player:
        flash("No active player.")
        return redirect(url_for("auction_room"))
    if not CURRENT_BID:
        flash("No bids to sell.")
        return redirect(url_for("auction_room"))

    team = TEAMS[CURRENT_BID.team_name]
    price_cr = CURRENT_BID.amount
    team.purse_remaining -= price_cr
    team.squad.append(player.id)
    SOLD_PLAYERS[player.id] = {
        "player_name": player.full_name,
        "team_name": team.name,
        "price_cr": price_cr,
    }
    flash(f"Sold {player.full_name} to {team.name} for {price_cr:.2f} Cr.")
    CURRENT_BID = None
    CURRENT_INDEX += 1
    return redirect(url_for("auction_room"))


@app.route("/auction/unsold", methods=["POST"])
def unsold_player():
    global CURRENT_INDEX, CURRENT_BID
    if not is_admin():
        return redirect(url_for("auction_room"))
    if not AUCTION_STARTED:
        flash("Auction is not live.")
        return redirect(url_for("auction_room"))
    player = get_current_player()
    if not player:
        flash("No active player.")
        return redirect(url_for("auction_room"))
    SOLD_PLAYERS[player.id] = {
        "player_name": player.full_name,
        "team_name": None,
        "price_cr": 0.0,
    }
    flash(f"{player.full_name} marked unsold.")
    CURRENT_BID = None
    CURRENT_INDEX += 1
    return redirect(url_for("auction_room"))


@app.route("/auction/next", methods=["POST"])
def next_player():
    global CURRENT_INDEX, CURRENT_BID
    if not is_admin():
        return redirect(url_for("auction_room"))
    if not AUCTION_STARTED:
        flash("Auction is not live.")
        return redirect(url_for("auction_room"))
    CURRENT_BID = None
    CURRENT_INDEX += 1
    flash("Moved to next player.")
    return redirect(url_for("auction_room"))


# ----------------- MAIN -----------------

if __name__ == "__main__":
    init_teams()
    load_players_auto()
    app.run(debug=True)
