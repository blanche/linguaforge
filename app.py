import os
import json
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

DUOLINGO_BASE = "https://www.duolingo.com"

# ─── Duolingo helpers ────────────────────────────────────────────────────────

def duo_headers(jwt: str) -> dict:
    return {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    }


def get_user_info(jwt: str):
    """Fetch /users/~me to get username + language data."""
    r = requests.get(
        f"{DUOLINGO_BASE}/api/1/users/~me",
        headers=duo_headers(jwt),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_vocabulary(jwt: str, learning_language: str, from_language: str = "en"):
    """Fetch vocabulary overview."""
    r = requests.get(
        f"{DUOLINGO_BASE}/vocabulary/overview",
        headers=duo_headers(jwt),
        params={"language_string": learning_language},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/connect", methods=["POST"])
def connect():
    """Verify JWT and return user info + language list."""
    data = request.get_json(force=True)
    jwt = (data.get("jwt") or "").strip()
    if not jwt:
        return jsonify({"error": "No JWT provided"}), 400

    try:
        info = get_user_info(jwt)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            return jsonify({"error": "Invalid or expired JWT. Please re-copy it from your browser."}), 401
        return jsonify({"error": f"Duolingo returned {e.response.status_code if e.response else 'error'}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    username = info.get("username") or info.get("name", "Unknown")
    # Collect courses
    courses = []
    for course in info.get("courses", []):
        courses.append({
            "id": course.get("id", ""),
            "learning": course.get("learningLanguage", ""),
            "from": course.get("fromLanguage", "en"),
            "title": course.get("title", course.get("learningLanguage", "")),
            "xp": course.get("xp", 0),
        })

    # Fallback: language_data keys
    if not courses:
        for lang_key in info.get("language_data", {}).keys():
            courses.append({"learning": lang_key, "from": "en", "title": lang_key, "xp": 0})

    return jsonify({"username": username, "courses": courses})


@app.route("/api/vocabulary", methods=["POST"])
def vocabulary():
    """Fetch vocabulary for a given language."""
    data = request.get_json(force=True)
    jwt = (data.get("jwt") or "").strip()
    language = (data.get("language") or "").strip()
    if not jwt or not language:
        return jsonify({"error": "jwt and language required"}), 400

    try:
        vocab_data = get_vocabulary(jwt, language)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response else 0
        if status in (401, 403):
            return jsonify({"error": "Unauthorized – check your JWT"}), 401
        return jsonify({"error": f"Duolingo API error {status}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    words = []
    for item in vocab_data.get("vocab_overview", []):
        words.append({
            "word": item.get("word_string", ""),
            "normalized": item.get("normalized_string", item.get("word_string", "")),
            "skill": item.get("skill", ""),
            "skill_url": item.get("skill_url_title", ""),
            "strength": round(item.get("strength", 0), 2),
            "last_practiced": item.get("last_practiced_ms", 0),
            "pos": item.get("pos", ""),
            "gender": item.get("gender", ""),
            "infinitive": item.get("infinitive", ""),
        })

    # Sort weakest first
    words.sort(key=lambda w: w["strength"])

    return jsonify({
        "language_string": vocab_data.get("language_string", language),
        "learning_language": vocab_data.get("learning_language", language),
        "from_language": vocab_data.get("from_language", "en"),
        "words": words,
        "total": len(words),
    })


@app.route("/api/translate", methods=["POST"])
def translate():
    """Use free LibreTranslate or fallback MyMemory to translate a word."""
    data = request.get_json(force=True)
    word = data.get("word", "")
    source = data.get("source", "es")
    target = data.get("target", "en")

    # Try MyMemory free API (no key needed)
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": word, "langpair": f"{source}|{target}"},
            timeout=8,
        )
        r.raise_for_status()
        result = r.json()
        translation = result.get("responseData", {}).get("translatedText", "")
        if translation and translation.lower() != word.lower():
            return jsonify({"translation": translation, "source": "MyMemory"})
    except Exception:
        pass

    return jsonify({"translation": "", "source": "none"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
