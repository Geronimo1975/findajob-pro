// Legal documents — German market (GDPR/DSGVO, DDG, TTDSG). DE version is legally binding.
// Mirrors GET /api/v1/legal/:doc/?lang=
export const LEGAL_DOCS = ['terms', 'privacy', 'cookies', 'impressum', 'withdrawal', 'dpa'];

export const LEGAL = {
  en: {
    _note: 'English translation for convenience. The German version (DE) is the legally binding one.',
    terms: { title: 'Terms & Conditions (AGB)', updated: 'Aug 1, 2026', sections: [
      ['1. Scope', 'These Terms govern the use of Findjob.pro, operated by George Sebastian Cucuiet, Greuner Brook 1, 21271 Hanstedt, Niedersachsen, Germany ("we"). By registering you accept these Terms. Deviating user terms do not apply.'],
      ['2. Services', 'Findjob.pro aggregates job listings from third-party platforms, generates AI-assisted application content and manages application documents. AI output is a draft: you are responsible for reviewing and sending every application. We do not guarantee interviews or employment.'],
      ['3. Account & permitted use', 'One account per person. You must not use the service to violate third-party platform terms (e.g. automated actions on LinkedIn or Indeed where prohibited), send spam, or upload unlawful content. We may suspend accounts for abuse.'],
      ['4. Subscriptions & payment', 'Free, Pro and Max plans as described on the Billing page. Paid plans renew monthly or yearly and are billed via Stripe. Prices include statutory German VAT where applicable. You may cancel at any time, effective at the end of the billing period (§ 327 et seq. BGB remains unaffected).'],
      ['5. Right of withdrawal', 'Consumers have a 14-day right of withdrawal — see the separate Withdrawal Policy (Widerrufsbelehrung).'],
      ['6. Liability', 'We are liable without limitation for intent and gross negligence. For slight negligence we are liable only for breach of essential contractual duties (Kardinalpflichten), limited to the foreseeable, contract-typical damage. Liability under the Product Liability Act remains unaffected.'],
      ['7. Final provisions', 'German law applies, excluding the UN Sales Convention. If you are a merchant, the place of jurisdiction is Hamburg. Should individual clauses be invalid, the remainder stays in force.'],
    ]},
    privacy: { title: 'Privacy Policy (Datenschutzerklärung)', updated: 'Aug 1, 2026', sections: [
      ['1. Controller', 'George Sebastian Cucuiet, Greuner Brook 1, 21271 Hanstedt, Niedersachsen, Germany. E-mail: george.cucuiet@gmx.de. Data Protection Officer: george.cucuiet@gmx.de.'],
      ['2. Data we process', 'Account data (name, e-mail), your master profile (CV data, skills, employment history), uploaded documents (CVs, certificates, diplomas), job preferences, application content, usage data and technical log data (IP address, browser).'],
      ['3. Purposes & legal bases (Art. 6 GDPR)', 'Contract performance — providing the tracker, generating applications (Art. 6 (1) b). Consent — sending applications on your behalf, connecting third-party APIs, optional cookies (Art. 6 (1) a, revocable at any time). Legitimate interest — service security and abuse prevention (Art. 6 (1) f).'],
      ['4. AI processing', 'Job descriptions and relevant profile data are transmitted to AI providers (Anthropic; optionally OpenAI) solely to generate your application drafts. We have data processing agreements (Art. 28 GDPR) in place; your data is not used to train their models.'],
      ['5. Recipients & processors', 'Hosting: IONOS SE (Germany). Payment: Stripe Payments Europe Ltd. Optional integrations you connect yourself: Canva, Telegram, WhatsApp (Meta), Jotform, Slack — each only after your explicit consent.'],
      ['6. Third-country transfers', 'Where processors are located outside the EEA (e.g. USA), transfers rely on EU Standard Contractual Clauses and the EU-US Data Privacy Framework (Art. 44 et seq. GDPR).'],
      ['7. Storage & security', 'Documents are encrypted at rest (AES-256) and in transit (TLS 1.3). Data is deleted upon account deletion, at the latest 30 days after, unless statutory retention duties apply (§ 147 AO, § 257 HGB).'],
      ['8. Your rights', 'Access (Art. 15), rectification (Art. 16), erasure (Art. 17), restriction (Art. 18), data portability (Art. 20), objection (Art. 21) and revocation of consent. Complaints: Hamburg Commissioner for Data Protection (HmbBfDI), Ludwig-Erhard-Str. 22, 20459 Hamburg.'],
    ]},
    cookies: { title: 'Cookie Policy', updated: 'Aug 1, 2026', sections: [
      ['1. Legal basis', 'Cookies and similar storage require consent under § 25 TTDSG, except those strictly necessary for the service (§ 25 (2) TTDSG).'],
      ['2. Essential cookies', 'Session cookie (login), CSRF protection, language preference, cookie-consent status. Lifetime: session up to 12 months. No consent required.'],
      ['3. Functional cookies (consent)', 'Saved dashboard view (table/board), collapsed sidebar state, last selected platform tab.'],
      ['4. Analytics (consent)', 'Self-hosted, cookieless-capable analytics (Matomo) with IP anonymisation. No data is shared with advertising networks. We use no marketing or tracking cookies.'],
      ['5. Managing consent', 'You can change or revoke your choices at any time via "Cookie settings" in the footer. Revocation does not affect the lawfulness of prior processing.'],
    ]},
    impressum: { title: 'Impressum (§ 5 DDG)', updated: 'Aug 1, 2026', sections: [
      ['Provider', 'George Sebastian Cucuiet\nGreuner Brook 1\n21271 Hanstedt, Niedersachsen, Germany'],
      ['Contact', 'E-mail: george.cucuiet@gmx.de · Phone: +49 160 4155546'],
      ['Responsible for content (§ 18 (2) MStV)', 'George Sebastian Cucuiet, address as above.'],
      ['EU dispute resolution', 'Platform of the EU Commission for online dispute resolution: https://ec.europa.eu/consumers/odr. We are neither obliged nor willing to participate in dispute resolution proceedings before a consumer arbitration board (§ 36 VSBG).'],
    ]},
    withdrawal: { title: 'Withdrawal Policy (Widerrufsbelehrung)', updated: 'Aug 1, 2026', sections: [
      ['Right of withdrawal', 'You may withdraw this contract within 14 days without giving reasons. The period begins on the day of contract conclusion. To exercise it, notify us (george.cucuiet@gmx.de) by a clear declaration; the model withdrawal form may be used.'],
      ['Consequences', 'We refund all payments received within 14 days using the original payment method. If you requested the service to start during the withdrawal period, you owe a proportionate amount for services already provided.'],
      ['Early expiry', 'The right expires when we have fully performed the service and you expressly consented to performance before the end of the withdrawal period (§ 356 (4) BGB).'],
    ]},
    dpa: { title: 'Data Processing Agreement (AVV, Art. 28 GDPR)', updated: 'Aug 1, 2026', sections: [
      ['For team workspaces', 'Where an organisation processes candidate data of third parties in Findjob.pro, we act as processor and offer a Data Processing Agreement pursuant to Art. 28 GDPR, including the list of sub-processors, technical and organisational measures (TOMs) and deletion duties. Request: george.cucuiet@gmx.de.'],
    ]},
  },
  de: {
    _note: 'Die deutsche Fassung ist rechtlich maßgeblich.',
    terms: { title: 'Allgemeine Geschäftsbedingungen (AGB)', updated: '1. Aug. 2026', sections: [
      ['1. Geltungsbereich', 'Diese AGB regeln die Nutzung von Findjob.pro, betrieben von George Sebastian Cucuiet, Greuner Brook 1, 21271 Hanstedt, Niedersachsen („wir"). Mit der Registrierung akzeptieren Sie diese AGB. Abweichende Bedingungen des Nutzers finden keine Anwendung.'],
      ['2. Leistungen', 'Findjob.pro aggregiert Stellenanzeigen von Drittplattformen, erstellt KI-gestützte Bewerbungsentwürfe und verwaltet Bewerbungsunterlagen. KI-Ausgaben sind Entwürfe: Die Prüfung und der Versand jeder Bewerbung liegen in Ihrer Verantwortung. Ein Vermittlungserfolg wird nicht geschuldet.'],
      ['3. Konto & zulässige Nutzung', 'Ein Konto pro Person. Untersagt sind Nutzungen, die gegen Bedingungen von Drittplattformen verstoßen (z. B. verbotene Automatisierung auf LinkedIn oder Indeed), Spam sowie rechtswidrige Inhalte. Bei Missbrauch können Konten gesperrt werden.'],
      ['4. Abonnements & Zahlung', 'Tarife Free, Pro und Max gemäß Abrechnungsseite. Bezahlte Tarife verlängern sich monatlich bzw. jährlich und werden über Stripe abgerechnet. Preise verstehen sich inkl. gesetzlicher USt. Kündigung jederzeit zum Ende des Abrechnungszeitraums; §§ 327 ff. BGB bleiben unberührt.'],
      ['5. Widerrufsrecht', 'Verbrauchern steht ein 14-tägiges Widerrufsrecht zu — siehe gesonderte Widerrufsbelehrung.'],
      ['6. Haftung', 'Wir haften unbeschränkt für Vorsatz und grobe Fahrlässigkeit. Bei einfacher Fahrlässigkeit haften wir nur für die Verletzung wesentlicher Vertragspflichten (Kardinalpflichten), begrenzt auf den vorhersehbaren, vertragstypischen Schaden. Die Haftung nach dem ProdHaftG bleibt unberührt.'],
      ['7. Schlussbestimmungen', 'Es gilt deutsches Recht unter Ausschluss des UN-Kaufrechts. Ist der Nutzer Kaufmann, ist Gerichtsstand Hamburg. Die Unwirksamkeit einzelner Klauseln berührt die Wirksamkeit im Übrigen nicht.'],
    ]},
    privacy: { title: 'Datenschutzerklärung', updated: '1. Aug. 2026', sections: [
      ['1. Verantwortlicher', 'George Sebastian Cucuiet, Greuner Brook 1, 21271 Hanstedt, Niedersachsen. E-Mail: george.cucuiet@gmx.de. Datenschutzbeauftragter: george.cucuiet@gmx.de.'],
      ['2. Verarbeitete Daten', 'Kontodaten (Name, E-Mail), Master-Profil (Lebenslaufdaten, Skills, Berufserfahrung), hochgeladene Dokumente (Lebensläufe, Zertifikate, Zeugnisse), Jobpräferenzen, Bewerbungsinhalte, Nutzungs- und technische Protokolldaten (IP-Adresse, Browser).'],
      ['3. Zwecke & Rechtsgrundlagen (Art. 6 DSGVO)', 'Vertragserfüllung — Bereitstellung des Trackers, Erstellung von Bewerbungen (Art. 6 Abs. 1 lit. b). Einwilligung — Versand von Bewerbungen in Ihrem Namen, Anbindung von Dritt-APIs, optionale Cookies (Art. 6 Abs. 1 lit. a, jederzeit widerruflich). Berechtigtes Interesse — Sicherheit und Missbrauchsprävention (Art. 6 Abs. 1 lit. f).'],
      ['4. KI-Verarbeitung', 'Stellenbeschreibungen und relevante Profildaten werden ausschließlich zur Erstellung Ihrer Bewerbungsentwürfe an KI-Anbieter (Anthropic; optional OpenAI) übermittelt. Auftragsverarbeitungsverträge (Art. 28 DSGVO) bestehen; Ihre Daten werden nicht zum Training der Modelle verwendet.'],
      ['5. Empfänger & Auftragsverarbeiter', 'Hosting: IONOS SE (Deutschland). Zahlungen: Stripe Payments Europe Ltd. Optionale, von Ihnen selbst verbundene Integrationen: Canva, Telegram, WhatsApp (Meta), Jotform, Slack — jeweils nur nach ausdrücklicher Einwilligung.'],
      ['6. Drittlandübermittlung', 'Soweit Auftragsverarbeiter außerhalb des EWR sitzen (z. B. USA), erfolgt die Übermittlung auf Grundlage der EU-Standardvertragsklauseln und des EU-US Data Privacy Framework (Art. 44 ff. DSGVO).'],
      ['7. Speicherung & Sicherheit', 'Dokumente werden verschlüsselt gespeichert (AES-256) und übertragen (TLS 1.3). Löschung bei Kontolöschung, spätestens nach 30 Tagen, soweit keine gesetzlichen Aufbewahrungspflichten bestehen (§ 147 AO, § 257 HGB).'],
      ['8. Ihre Rechte', 'Auskunft (Art. 15), Berichtigung (Art. 16), Löschung (Art. 17), Einschränkung (Art. 18), Datenübertragbarkeit (Art. 20), Widerspruch (Art. 21), Widerruf von Einwilligungen. Beschwerde: Hamburgischer Beauftragter für Datenschutz und Informationsfreiheit (HmbBfDI), Ludwig-Erhard-Str. 22, 20459 Hamburg.'],
    ]},
    cookies: { title: 'Cookie-Richtlinie', updated: '1. Aug. 2026', sections: [
      ['1. Rechtsgrundlage', 'Cookies und ähnliche Speichertechniken bedürfen der Einwilligung nach § 25 TTDSG, ausgenommen unbedingt erforderliche (§ 25 Abs. 2 TTDSG).'],
      ['2. Essenzielle Cookies', 'Session-Cookie (Login), CSRF-Schutz, Spracheinstellung, Consent-Status. Laufzeit: Session bis 12 Monate. Keine Einwilligung erforderlich.'],
      ['3. Funktionale Cookies (Einwilligung)', 'Gespeicherte Dashboard-Ansicht (Tabelle/Board), eingeklappte Seitenleiste, zuletzt gewählter Plattform-Tab.'],
      ['4. Analyse (Einwilligung)', 'Selbst gehostetes Matomo mit IP-Anonymisierung. Keine Weitergabe an Werbenetzwerke. Es werden keine Marketing- oder Tracking-Cookies eingesetzt.'],
      ['5. Verwaltung der Einwilligung', 'Ihre Auswahl können Sie jederzeit über „Cookie-Einstellungen" im Footer ändern oder widerrufen. Der Widerruf berührt nicht die Rechtmäßigkeit der bisherigen Verarbeitung.'],
    ]},
    impressum: { title: 'Impressum (§ 5 DDG)', updated: '1. Aug. 2026', sections: [
      ['Anbieter', 'George Sebastian Cucuiet\nGreuner Brook 1\n21271 Hanstedt, Niedersachsen'],
      ['Kontakt', 'E-Mail: george.cucuiet@gmx.de · Telefon: +49 160 4155546'],
      ['Inhaltlich verantwortlich (§ 18 Abs. 2 MStV)', 'George Sebastian Cucuiet, Anschrift wie oben.'],
      ['EU-Streitschlichtung', 'Plattform der EU-Kommission zur Online-Streitbeilegung: https://ec.europa.eu/consumers/odr. Zur Teilnahme an Streitbeilegungsverfahren vor einer Verbraucherschlichtungsstelle sind wir nicht verpflichtet und nicht bereit (§ 36 VSBG).'],
    ]},
    withdrawal: { title: 'Widerrufsbelehrung', updated: '1. Aug. 2026', sections: [
      ['Widerrufsrecht', 'Sie können diesen Vertrag binnen 14 Tagen ohne Angabe von Gründen widerrufen. Die Frist beginnt mit Vertragsschluss. Der Widerruf erfolgt durch eindeutige Erklärung (george.cucuiet@gmx.de); das Muster-Widerrufsformular kann verwendet werden.'],
      ['Folgen des Widerrufs', 'Erhaltene Zahlungen erstatten wir binnen 14 Tagen über das ursprüngliche Zahlungsmittel. Haben Sie den Beginn der Leistung während der Widerrufsfrist verlangt, schulden Sie einen anteiligen Betrag für bereits erbrachte Leistungen.'],
      ['Vorzeitiges Erlöschen', 'Das Widerrufsrecht erlischt, wenn wir die Leistung vollständig erbracht haben und Sie der Ausführung vor Fristablauf ausdrücklich zugestimmt haben (§ 356 Abs. 4 BGB).'],
    ]},
    dpa: { title: 'Auftragsverarbeitungsvertrag (AVV, Art. 28 DSGVO)', updated: '1. Aug. 2026', sections: [
      ['Für Team-Workspaces', 'Verarbeitet eine Organisation in Findjob.pro Kandidatendaten Dritter, handeln wir als Auftragsverarbeiter und bieten einen AVV nach Art. 28 DSGVO an — inkl. Subunternehmerliste, technisch-organisatorischer Maßnahmen (TOMs) und Löschpflichten. Anfrage: george.cucuiet@gmx.de.'],
    ]},
  },
  ro: {
    _note: 'Traducere în limba română pentru conveniență. Versiunea germană (DE) este cea obligatorie juridic.',
    terms: { title: 'Termeni și condiții (AGB)', updated: '1 aug. 2026', sections: [
      ['1. Domeniu de aplicare', 'Acești termeni reglementează utilizarea Findjob.pro, operat de George Sebastian Cucuiet, Greuner Brook 1, 21271 Hanstedt, Niedersachsen, Germania. Prin înregistrare acceptați acești termeni.'],
      ['2. Servicii', 'Findjob.pro agregă joburi din platforme terțe, generează conținut de aplicare asistat de AI și gestionează documentele. Conținutul AI este un draft: verificarea și trimiterea fiecărei aplicări sunt responsabilitatea dvs. Nu garantăm interviuri sau angajare.'],
      ['3. Cont & utilizare permisă', 'Un cont per persoană. Este interzisă utilizarea care încalcă termenii platformelor terțe (ex. automatizare interzisă pe LinkedIn sau Indeed), spamul sau conținutul ilegal.'],
      ['4. Abonamente & plată', 'Planuri Free, Pro și Max conform paginii de Billing. Planurile plătite se reînnoiesc lunar/anual, facturate prin Stripe, cu TVA german inclus. Puteți anula oricând, cu efect la finalul perioadei de facturare.'],
      ['5. Drept de retragere', 'Consumatorii au drept de retragere de 14 zile — vezi Politica de retragere (Widerrufsbelehrung).'],
      ['6. Răspundere', 'Răspundem nelimitat pentru intenție și neglijență gravă; pentru neglijență ușoară doar la încălcarea obligațiilor contractuale esențiale, limitat la prejudiciul previzibil, tipic contractului.'],
      ['7. Dispoziții finale', 'Se aplică legea germană, cu excluderea Convenției ONU privind vânzarea. Pentru comercianți, instanța competentă este Hamburg.'],
    ]},
    privacy: { title: 'Politica de confidențialitate (Datenschutzerklärung)', updated: '1 aug. 2026', sections: [
      ['1. Operator', 'George Sebastian Cucuiet, Greuner Brook 1, 21271 Hanstedt, Niedersachsen, Germania. E-mail: george.cucuiet@gmx.de. Responsabil cu protecția datelor: george.cucuiet@gmx.de.'],
      ['2. Date prelucrate', 'Date de cont (nume, e-mail), profilul master (CV, competențe, experiență), documente încărcate (CV-uri, certificate, diplome), preferințe de job, conținutul aplicărilor, date de utilizare și jurnal tehnic (IP, browser).'],
      ['3. Scopuri & temeiuri legale (art. 6 RGPD)', 'Executarea contractului — furnizarea trackerului, generarea aplicărilor (art. 6 (1) b). Consimțământ — trimiterea aplicărilor în numele dvs., conectarea API-urilor terțe, cookie-uri opționale (art. 6 (1) a, revocabil oricând). Interes legitim — securitate și prevenirea abuzului (art. 6 (1) f).'],
      ['4. Prelucrare AI', 'Descrierile joburilor și datele de profil relevante sunt transmise furnizorilor AI (Anthropic; opțional OpenAI) exclusiv pentru generarea drafturilor. Există acorduri de prelucrare (art. 28 RGPD); datele dvs. nu sunt folosite la antrenarea modelelor.'],
      ['5. Destinatari & împuterniciți', 'Găzduire: IONOS SE (Germania). Plăți: Stripe Payments Europe Ltd. Integrări opționale conectate de dvs.: Canva, Telegram, WhatsApp (Meta), Jotform, Slack — doar cu consimțământ explicit.'],
      ['6. Transferuri în țări terțe', 'Pentru împuterniciți din afara SEE (ex. SUA), transferurile se bazează pe Clauzele Contractuale Standard UE și EU-US Data Privacy Framework (art. 44 și urm. RGPD).'],
      ['7. Stocare & securitate', 'Documentele sunt criptate în repaus (AES-256) și în tranzit (TLS 1.3). Ștergere la închiderea contului, cel târziu după 30 de zile, cu excepția obligațiilor legale de păstrare.'],
      ['8. Drepturile dvs.', 'Acces (art. 15), rectificare (art. 16), ștergere (art. 17), restricționare (art. 18), portabilitate (art. 20), opoziție (art. 21), revocarea consimțământului. Plângeri: HmbBfDI, Ludwig-Erhard-Str. 22, 20459 Hamburg.'],
    ]},
    cookies: { title: 'Politica privind cookie-urile', updated: '1 aug. 2026', sections: [
      ['1. Temei legal', 'Cookie-urile necesită consimțământ conform § 25 TTDSG, cu excepția celor strict necesare (§ 25 (2) TTDSG).'],
      ['2. Cookie-uri esențiale', 'Sesiune (login), protecție CSRF, limba, statusul consimțământului. Durată: sesiune până la 12 luni. Fără consimțământ necesar.'],
      ['3. Funcționale (consimțământ)', 'Vizualizarea salvată (tabel/board), starea sidebar-ului, ultimul tab de platformă.'],
      ['4. Analiză (consimțământ)', 'Matomo self-hosted cu anonimizarea IP-ului. Fără rețele de publicitate, fără cookie-uri de marketing sau tracking.'],
      ['5. Gestionarea consimțământului', 'Puteți modifica sau revoca alegerile oricând din „Setări cookie" (footer). Revocarea nu afectează legalitatea prelucrării anterioare.'],
    ]},
    impressum: { title: 'Impressum (§ 5 DDG)', updated: '1 aug. 2026', sections: [
      ['Furnizor', 'George Sebastian Cucuiet\nGreuner Brook 1\n21271 Hanstedt, Niedersachsen, Germania'],
      ['Contact', 'E-mail: george.cucuiet@gmx.de · Telefon: +49 160 4155546'],
      ['Responsabil pentru conținut (§ 18 (2) MStV)', 'George Sebastian Cucuiet, adresa de mai sus.'],
      ['Soluționarea litigiilor UE', 'Platforma Comisiei Europene pentru soluționarea online a litigiilor: https://ec.europa.eu/consumers/odr. Nu suntem obligați și nu dorim să participăm la proceduri în fața unei comisii de arbitraj pentru consumatori (§ 36 VSBG).'],
    ]},
    withdrawal: { title: 'Politica de retragere (Widerrufsbelehrung)', updated: '1 aug. 2026', sections: [
      ['Dreptul de retragere', 'Puteți denunța contractul în 14 zile fără motivare. Termenul curge de la încheierea contractului. Retragerea se face printr-o declarație clară (george.cucuiet@gmx.de).'],
      ['Consecințe', 'Rambursăm plățile primite în 14 zile, prin metoda de plată originală. Dacă ați cerut începerea serviciului în perioada de retragere, datorați o sumă proporțională pentru serviciile deja prestate.'],
      ['Expirare anticipată', 'Dreptul se stinge când serviciul a fost prestat integral și ați consimțit expres la executare înainte de expirarea termenului (§ 356 (4) BGB).'],
    ]},
    dpa: { title: 'Acord de prelucrare a datelor (AVV, art. 28 RGPD)', updated: '1 aug. 2026', sections: [
      ['Pentru workspace-uri de echipă', 'Când o organizație prelucrează în Findjob.pro date ale unor candidați terți, acționăm ca persoană împuternicită și oferim un AVV conform art. 28 RGPD — cu lista subîmputerniciților, măsurile tehnice și organizatorice (TOMs) și obligațiile de ștergere. Solicitări: george.cucuiet@gmx.de.'],
    ]},
  },
};
