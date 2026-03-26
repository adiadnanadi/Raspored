import os
import json
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"

SYSTEM_PROMPT = """Ti si ekspertni sistem za generisanje školskih rasporeda časova.
Tvoj zadatak je kreirati OPTIMALAN i VALIDAN raspored koji zadovoljava SVA ograničenja.

STROGA PRAVILA koja NIKADA ne smiješ prekršiti:
1. Profesor NE MOŽE držati nastavu u dva odjeljenja u isto vrijeme
2. Kabinet NE MOŽE biti korišten od dva odjeljenja u isto vrijeme
3. Odjeljenje NE MOŽE imati dva predmeta u istom terminu
4. Eksterni profesori mogu raditi SAMO u terminima kad su DOSTUPNI
5. Svaki predmet mora imati TAČNO onoliko časova sedmično koliko je navedeno
6. Rasporedi časove RAVNOMJERNO kroz sedmicu (ne stavljaj sve časove istog predmeta u jedan dan)
7. Izbjegavaj rupe u rasporedu odjeljenja — časovi trebaju biti uzastopni od prvog časa
8. Ako predmet zahtijeva poseban kabinet (laboratorija, sala), koristi odgovarajući kabinet

Odgovori ISKLJUČIVO validnim JSON formatom bez dodatnog teksta.
Format odgovora:
{
  "raspored": {
    "NAZIV_ODJELJENJA": {
      "Ponedjeljak": [
        {"cas": 1, "predmet": "Naziv predmeta", "profesor": "Ime profesora", "kabinet": "Oznaka kabineta"},
        {"cas": 2, "predmet": "...", "profesor": "...", "kabinet": "..."}
      ],
      "Utorak": [...],
      "Srijeda": [...],
      "Cetvrtak": [...],
      "Petak": [...]
    }
  },
  "napomene": ["Lista eventualnih napomena ili upozorenja"]
}

Ako je čas prazan/slobodan, stavi: {"cas": N, "predmet": "-", "profesor": "-", "kabinet": "-"}
"""


def napravi_prompt(data):
    profesori = data.get("profesori", [])
    predmeti = data.get("predmeti", [])
    odjeljenja = data.get("odjeljenja", [])
    kabineti = data.get("kabineti", [])
    postavke = data.get("postavke", {})
    fond = data.get("fond_casova", {})

    dani = postavke.get("dani", [
        "Ponedjeljak", "Utorak", "Srijeda", "Cetvrtak", "Petak"
    ])
    broj_casova = postavke.get("broj_casova", 7)

    prompt = f"""Generiši raspored časova za školu sa sljedećim parametrima:

═══ RADNI DANI ═══
{', '.join(dani)}

═══ BROJ ČASOVA DNEVNO ═══
{broj_casova} časova po danu

═══ ODJELJENJA ═══
{', '.join(odjeljenja)}

═══ KABINETI ═══
"""
    for k in kabineti:
        tip = f" (tip: {k['tip']})" if k.get("tip") else ""
        prompt += f"- {k['naziv']}{tip}\n"

    prompt += "\n═══ PREDMETI ═══\n"
    for p in predmeti:
        kab = f" [ZAHTIJEVA KABINET TIPA: {p['tip_kabineta']}]" \
            if p.get("tip_kabineta") else ""
        prompt += f"- {p['naziv']}{kab}\n"

    prompt += "\n═══ PROFESORI ═══\n"
    for p in profesori:
        subjects = ", ".join(p.get("predmeti", []))
        status = "STALNI (uvijek dostupan)"
        if p.get("eksterni"):
            status = "EKSTERNI"
            if p.get("nedostupan"):
                nedostupni_termini = []
                for dan, casovi in p["nedostupan"].items():
                    if casovi:
                        nedostupni_termini.append(
                            f"{dan}: časovi {','.join(map(str, casovi))}"
                        )
                if nedostupni_termini:
                    status += (
                        " — NEDOSTUPAN u: "
                        + "; ".join(nedostupni_termini)
                    )
        prompt += f"- {p['ime']} [{status}] — predaje: {subjects}\n"

    prompt += "\n═══ FOND ČASOVA (predmet → broj časova sedmično "
    prompt += "po odjeljenju) ═══\n"
    for odjeljenje, predmeti_fond in fond.items():
        prompt += f"\n{odjeljenje}:\n"
        for predmet, sati in predmeti_fond.items():
            if sati and int(sati) > 0:
                prompt += f"  - {predmet}: {sati} časova sedmično\n"

    prompt += """
═══ ZADATAK ═══
Generiši KOMPLETAN raspored za SVA odjeljenja.
Provjeri da NIJEDAN profesor nema konflikt.
Provjeri da NIJEDAN kabinet nema konflikt.
Poštuj dostupnost eksternih profesora.
Vrati SAMO JSON, bez dodatnog teksta.
"""
    return prompt


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "api_key": "postavljen" if MISTRAL_KEY else "NEDOSTAJE",
        "model": MISTRAL_MODEL,
    })


@app.route("/generate", methods=["POST"])
def generate():
    if not MISTRAL_KEY:
        return jsonify({
            "success": False,
            "error": "MISTRAL_API_KEY nije postavljen u Environment Variables"
        }), 500

    data = request.get_json()

    if not data.get("odjeljenja"):
        return jsonify({
            "success": False,
            "error": "Dodaj barem jedno odjeljenje"
        }), 400
    if not data.get("profesori"):
        return jsonify({
            "success": False,
            "error": "Dodaj barem jednog profesora"
        }), 400
    if not data.get("predmeti"):
        return jsonify({
            "success": False,
            "error": "Dodaj barem jedan predmet"
        }), 400

    prompt = napravi_prompt(data)

    try:
        resp = requests.post(
            MISTRAL_URL,
            headers={
                "Authorization": f"Bearer {MISTRAL_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MISTRAL_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.15,
                "max_tokens": 16000,
                "response_format": {"type": "json_object"},
            },
            timeout=180,
        )
        resp.raise_for_status()

        content = resp.json()["choices"][0]["message"]["content"]
        raspored_data = json.loads(content)

        return jsonify({"success": True, "data": raspored_data})

    except requests.exceptions.Timeout:
        return jsonify({
            "success": False,
            "error": "Timeout — prevelik raspored. Smanji broj odjeljenja "
                     "ili predmeta pa probaj ponovo."
        }), 504
    except requests.exceptions.HTTPError as e:
        error_detail = ""
        try:
            error_detail = e.response.json().get("message", str(e))
        except Exception:
            error_detail = str(e)
        return jsonify({
            "success": False,
            "error": f"Mistral API greška: {error_detail}"
        }), 502
    except json.JSONDecodeError:
        return jsonify({
            "success": False,
            "error": "Mistral nije vratio validan JSON — probaj ponovo"
        }), 500
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Greška: {str(e)}"
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
