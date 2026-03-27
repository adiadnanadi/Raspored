import os
import re
import json
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-large-latest")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


SYSTEM_PROMPT = """Ti si ekspertni sistem za pravljenje školskih rasporeda.

APSOLUTNA PRAVILA (nikad ne prekrši):
1. JEDAN profesor NE MOŽE biti u DVA odjeljenja u ISTO VRIJEME (isti dan, isti čas)
2. JEDAN kabinet NE MOŽE biti korišten od DVA odjeljenja u ISTO VRIJEME
3. Odjeljenje NE MOŽE imati DVA predmeta u ISTOM terminu
4. Eksterni profesor radi SAMO kad je DOSTUPAN
5. Svaki predmet mora imati TAČAN broj časova sedmično koliko je zadano
6. Časovi trebaju biti uzastopni (bez rupa)
7. Rasporedi časove RAVNOMJERNO kroz sedmicu

PROCEDURA:
1. Prvo rasporedi predmete sa eksternim profesorima (imaju ograničenja)
2. Zatim rasporedi ostale predmete
3. PROVJERI svaki termin da nema konflikata
4. Ako profesor predaje u I-1 u Ponedjeljak 1. čas, NE MOŽE biti u I-2 u Ponedjeljak 1. čas

ODGOVORI ISKLJUČIVO ČISTIM JSON OBJEKTOM. BEZ MARKDOWN-A, BEZ ```json, BEZ TEKSTA PRIJE ILI POSLIJE.

Format:
{"raspored":{"ODJELJENJE":{"Dan":[{"cas":1,"predmet":"X","profesor":"Y","kabinet":"Z"}]}},"napomene":["..."]}

Ako je čas slobodan: {"cas":N,"predmet":"-","profesor":"-","kabinet":"-"}"""


def extract_json(text):
    """Izvuci JSON iz odgovora, čak i ako je umotan u markdown."""
    text = text.strip()

    # Ukloni markdown code block ako postoji
    patterns = [
        r'```json\s*(.*?)\s*```',
        r'```\s*(.*?)\s*```',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            text = match.group(1).strip()
            break

    # Probaj naći JSON objekat
    # Nađi prvi { i zadnji }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return json.loads(text)


def validiraj_raspored(raspored_data, postavke):
    """Provjeri da nema preklapanja profesora i kabineta."""
    raspored = raspored_data.get("raspored", raspored_data)
    dani = postavke.get("dani", [])
    greske = []

    # Za svaki dan i čas, provjeri konflikte
    for dan in dani:
        # Nađi odgovarajući ključ dana u rasporedu
        max_cas = postavke.get("broj_casova", 7)

        for cas_num in range(1, max_cas + 1):
            profesori_u_terminu = {}
            kabineti_u_terminu = {}

            for odjeljenje, dani_data in raspored.items():
                # Nađi dan ključ
                dan_key = None
                for k in dani_data.keys():
                    if k.lower()[:3] == dan.lower()[:3]:
                        dan_key = k
                        break
                if not dan_key:
                    continue

                casovi = dani_data.get(dan_key, [])
                for cas in casovi:
                    if cas.get("cas") != cas_num:
                        continue
                    if cas.get("predmet", "-") == "-":
                        continue

                    prof = cas.get("profesor", "-")
                    kab = cas.get("kabinet", "-")

                    # Provjeri profesor konflikt
                    if prof != "-" and prof in profesori_u_terminu:
                        greske.append(
                            f"KONFLIKT: {prof} je u {dan} {cas_num}. čas "
                            f"i u {profesori_u_terminu[prof]} i u {odjeljenje}"
                        )
                    elif prof != "-":
                        profesori_u_terminu[prof] = odjeljenje

                    # Provjeri kabinet konflikt
                    if kab != "-" and kab in kabineti_u_terminu:
                        greske.append(
                            f"KONFLIKT: Kabinet {kab} je u {dan} {cas_num}. čas "
                            f"i u {kabineti_u_terminu[kab]} i u {odjeljenje}"
                        )
                    elif kab != "-":
                        kabineti_u_terminu[kab] = odjeljenje

    return greske


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

    prompt = f"""Napravi raspored za školu:

DANI: {', '.join(dani)}
ČASOVA DNEVNO: {broj_casova}
ODJELJENJA: {', '.join(odjeljenja)}

KABINETI:
"""
    for k in kabineti:
        tip = f" (tip: {k['tip']})" if k.get("tip") else ""
        prompt += f"  - {k['naziv']}{tip}\n"

    prompt += "\nPREDMETI:\n"
    for p in predmeti:
        kab = f" [ZAHTIJEVA: {p['tip_kabineta']}]" if p.get("tip_kabineta") else ""
        prompt += f"  - {p['naziv']}{kab}\n"

    prompt += "\nPROFESORI:\n"
    for p in profesori:
        subjects = ", ".join(p.get("predmeti", []))
        if p.get("eksterni"):
            status = "EKSTERNI"
            if p.get("nedostupan"):
                nedost = []
                for dan, casovi in p["nedostupan"].items():
                    if casovi:
                        nedost.append(f"{dan}: nedostupan {','.join(map(str, casovi))}. čas")
                if nedost:
                    status += " | " + "; ".join(nedost)
            prompt += f"  - {p['ime']} [{status}] predaje: {subjects}\n"
        else:
            prompt += f"  - {p['ime']} [STALNI - uvijek dostupan] predaje: {subjects}\n"

    prompt += "\nFOND ČASOVA SEDMIČNO:\n"
    for odjeljenje in odjeljenja:
        predmeti_fond = fond.get(odjeljenje, {})
        aktivni = {k: v for k, v in predmeti_fond.items() if v and int(v) > 0}
        if aktivni:
            prompt += f"\n  {odjeljenje}:\n"
            for predmet, sati in aktivni.items():
                prompt += f"    {predmet}: {sati} čas/sed\n"

    prompt += f"""
KRITIČNO PRAVILO:
Imam {len(odjeljenja)} odjeljenja. Svaki profesor koji predaje u više odjeljenja
NE SMIJE imati isti termin (dan+čas) u dva odjeljenja.

Primjer: Ako prof. Hodžić predaje Matematiku u I-1 Ponedjeljak 1. čas,
onda Hodžić NE MOŽE biti nigdje drugo u Ponedjeljak 1. čas.

Vrati ČIST JSON objekat (bez ```json, bez markdown-a).
"""
    return prompt


def pozovi_mistral(messages, attempt=1):
    """Pozovi Mistral API sa retry logikom."""
    try:
        resp = requests.post(
            MISTRAL_URL,
            headers={
                "Authorization": f"Bearer {MISTRAL_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MISTRAL_MODEL,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 16000,
            },
            timeout=180,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        print(f"[MISTRAL] Attempt {attempt} - Odgovor dužine: {len(content)}")
        print(f"[MISTRAL] Prvih 200 znakova: {content[:200]}")
        return content
    except requests.exceptions.HTTPError as e:
        error_detail = ""
        try:
            error_detail = e.response.json().get("message", str(e))
        except Exception:
            error_detail = str(e)
        raise Exception(f"Mistral API greška: {error_detail}")
    except requests.exceptions.Timeout:
        raise Exception("Timeout - prevelik raspored")


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
            "error": "MISTRAL_API_KEY nije postavljen"
        }), 500

    data = request.get_json()

    if not data.get("odjeljenja"):
        return jsonify({"success": False, "error": "Dodaj odjeljenja"}), 400
    if not data.get("profesori"):
        return jsonify({"success": False, "error": "Dodaj profesore"}), 400
    if not data.get("predmeti"):
        return jsonify({"success": False, "error": "Dodaj predmete"}), 400
    if not data.get("kabineti"):
        return jsonify({"success": False, "error": "Dodaj kabinete"}), 400

    prompt = napravi_prompt(data)
    postavke = data.get("postavke", {})

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\n[GENERATE] ═══ Pokušaj {attempt}/{max_attempts} ═══")

            content = pozovi_mistral(messages, attempt)

            # Parsiraj JSON
            try:
                raspored_data = extract_json(content)
            except json.JSONDecodeError as je:
                print(f"[GENERATE] JSON parse error: {je}")
                print(f"[GENERATE] Raw content:\n{content[:500]}")

                if attempt < max_attempts:
                    # Dodaj poruku da popravi format
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": "GREŠKA: Tvoj odgovor NIJE validan JSON. "
                                   "Vrati SAMO čist JSON objekat. "
                                   "Bez ```json, bez markdown-a, bez teksta. "
                                   "Počni sa { i završi sa }."
                    })
                    continue
                else:
                    return jsonify({
                        "success": False,
                        "error": "AI nije vratio validan JSON nakon 3 pokušaja. "
                                 "Probaj smanjiti broj odjeljenja ili predmeta."
                    }), 500

            # Validiraj raspored
            greske = validiraj_raspored(raspored_data, postavke)

            if greske:
                print(f"[GENERATE] Pronađeno {len(greske)} konflikata:")
                for g in greske:
                    print(f"  ❌ {g}")

                if attempt < max_attempts:
                    # Traži od Mistrala da popravi
                    messages.append({"role": "assistant", "content": content})
                    fix_msg = (
                        "GREŠKA! Raspored ima konflikte:\n\n"
                        + "\n".join(greske)
                        + "\n\nPOPRAVI SVE KONFLIKTE. "
                        "Profesor NE SMIJE biti u dva odjeljenja u isto vrijeme. "
                        "Kabinet NE SMIJE biti korišten od dva odjeljenja u isto vrijeme. "
                        "Premjesti konfliktne časove u druge termine. "
                        "Vrati KOMPLETAN popravljen raspored kao čist JSON."
                    )
                    messages.append({"role": "user", "content": fix_msg})
                    continue
                else:
                    # Vrati sa upozorenjima
                    if "napomene" not in raspored_data:
                        raspored_data["napomene"] = []
                    raspored_data["napomene"].extend(greske)
                    return jsonify({
                        "success": True,
                        "data": raspored_data
                    })

            # Sve OK!
            print(f"[GENERATE] ✅ Raspored validan! Pokušaj {attempt}")
            return jsonify({"success": True, "data": raspored_data})

        except Exception as e:
            print(f"[GENERATE] Greška na pokušaju {attempt}: {e}")
            if attempt >= max_attempts:
                return jsonify({
                    "success": False,
                    "error": str(e)
                }), 500

    return jsonify({
        "success": False,
        "error": "Nije uspjelo nakon svih pokušaja"
    }), 500


@app.route("/validate", methods=["POST"])
def validate():
    """Endpoint za ručnu validaciju rasporeda."""
    data = request.get_json()
    postavke = data.get("postavke", {})
    greske = validiraj_raspored(data, postavke)
    return jsonify({
        "valid": len(greske) == 0,
        "greske": greske,
        "ukupno_gresaka": len(greske)
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
