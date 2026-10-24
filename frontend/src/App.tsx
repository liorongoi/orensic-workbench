import { lazy, Suspense, useEffect, useState } from "react";
import { api, auth, setExaminer, type AuthStatus } from "./api";
import { CaseList } from "./components/CaseList";
import { JobQueue } from "./components/JobQueue";
import { ExaminerMenu } from "./components/ExaminerMenu";
import { MenuBar } from "./components/MenuBar";
import { Workspace } from "./components/Workspace";
import { TrafficPage } from "./components/TrafficPage";
import { AccountMenu, SignIn } from "./components/SignIn";
import type { AnalysisSchema, FilterSchema, User } from "./types";
import logo from "./assets/lucida.svg";
import { readHelpHash } from "./help";

const HelpPage = lazy(() => import("./components/HelpPage"));

function readHash() {
  const m = location.hash.match(/^#\/case\/([\w-]+)/);
  return m ? m[1] : null;
}

/** `#/case/<id>?ev=<evidence>&layout=viewer`: a second window showing only the viewer. */
/** `#/case/<id>/traffic`: the still-image traffic detection page for the case. */
function readTraffic() {
  return /^#\/case\/[\w-]+\/traffic/.test(location.hash);
}

function readHashOptions() {
  const q = new URLSearchParams(location.hash.split("?")[1] ?? "");
  return { evidence: q.get("ev"), viewerOnly: q.get("layout") === "viewer" };
}

export default function App() {
  const [me, setMe] = useState<User | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [caseId, setCaseId] = useState<string | null>(readHash());
  const [trafficView, setTrafficView] = useState(readTraffic());
  const [filters, setFilters] = useState<FilterSchema[]>([]);
  const [analyses, setAnalyses] = useState<AnalysisSchema[]>([]);
  const [caseName, setCaseName] = useState("");
  const [help, setHelp] = useState(readHelpHash() !== null);
  const [authState, setAuthState] = useState<AuthStatus | null>(null);

  const named = (u: User) => {
    setExaminer(u.full_name);
    setMe(u);
  };

  // By default there is no sign-in: the app opens straight away as the built-in local examiner.
  // A shared server (FORENSIC_AUTH=accounts) asks people to sign in first.
  const start = () =>
    auth
      .status()
      .then((s) => {
        setAuthState(s);
        if (s.mode === "none") return api.me().then(named);
        if (s.user) named(s.user);
        else setMe(null);
      })
      .catch((e) => setLoadError((e as Error).message));

  useEffect(() => {
    start();
    const signedOut = () => {
      setMe(null);
      start();
    };
    window.addEventListener("lucida-signed-out", signedOut);
    const onHash = () => {
      setCaseId(readHash());
      setTrafficView(readTraffic());
      setHelp(readHelpHash() !== null);
    };
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("lucida-signed-out", signedOut);
    };
  }, []);

  useEffect(() => {
    if (!me) return;
    api.filters().then(setFilters);
    api.analyses().then(setAnalyses);
  }, [me?.username]);

  useEffect(() => {
    if (me && caseId) api.caseDetail(caseId).then((d) => setCaseName(d.case.name)).catch(() => setCaseName(""));
  }, [caseId, me?.username]);

  // Help opens on its own: the guides are part of the app.
  if (help) {
    return (
      <div className="app">
        <header className="topbar">
          <a href="#/" className="brand">
            <img src={logo} alt="" width={22} height={22} />
            <span className="wordmark">Lucida</span>
          </a>
          <MenuBar caseId={null} signedIn={!!me} />
          <span className="crumb">/ Help</span>
          <span className="spacer" />
          <a className="button" href="#/">Back to Lucida</a>
        </header>
        <Suspense fallback={<p className="loading muted">Loading…</p>}>
          <HelpPage />
        </Suspense>
      </div>
    );
  }

  if (!me && authState?.mode === "accounts")
    return <SignIn needsSetup={!!authState.needs_setup} onSignedIn={(u) => { named(u); setAuthState({ ...authState, needs_setup: false, user: u }); }} />;
  if (!me) return <p className="loading muted">{loadError ? `Lucida could not start: ${loadError}` : "Loading…"}</p>;

  return (
    <div className="app">
      <header className="topbar">
        <a href="#/" className="brand">
          <img src={logo} alt="" width={22} height={22} />
          <span className="wordmark">Lucida</span>
        </a>
        <MenuBar caseId={caseId} signedIn />
        {caseId && <span className="crumb">/ {caseName}{trafficView ? " / Traffic" : ""}</span>}
        {caseId && (
          <a className="button" href={trafficView ? `#/case/${caseId}` : `#/case/${caseId}/traffic`}
            title={trafficView ? "Back to the forensic workspace" : "Vehicle detection and review on still images"}>
            {trafficView ? "Workspace" : "Traffic"}
          </a>
        )}
        <span className="spacer" />
        <JobQueue me={me} />
        {authState?.mode === "accounts" ? (
          <AccountMenu me={me} onSignedOut={() => { setMe(null); start(); }} />
        ) : (
          <ExaminerMenu me={me} onRenamed={named} />
        )}
      </header>
      {caseId && trafficView ? (
        <TrafficPage key={caseId} caseId={caseId} />
      ) : caseId ? (
        <Workspace key={caseId} caseId={caseId} filters={filters} analyses={analyses} {...readHashOptions()} />
      ) : (
        <CaseList onOpen={(id) => (location.hash = `#/case/${id}`)} />
      )}
    </div>
  );
}
