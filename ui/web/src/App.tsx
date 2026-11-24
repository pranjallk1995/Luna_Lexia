import { Cloud, Eye, Heart, LayoutGrid, List, Lock, LogOut, Mail, Upload, type LucideIcon } from "lucide-react";
import { useState } from "react";

import { useDriveStatus, useGmailStatus, type Section, type Settings } from "./api";
import { Dialog } from "./components/Dialog";
import { DrivePanel } from "./components/DrivePanel";
import { Gallery } from "./components/Gallery";
import { HiddenGate } from "./components/HiddenGate";
import { Brand, ForbiddenPage, SignInPage, UnreachablePage } from "./components/Gate";
import { GmailPanel } from "./components/GmailPanel";
import { UploadDialog } from "./components/UploadDialog";
import { useSession } from "./http";
import { remember, remembered } from "./lib";
import { useUploads } from "./uploads";

const SECTIONS: { id: Section; label: string; icon: LucideIcon }[] = [
  { id: "view", label: "View Documents", icon: Eye },
  { id: "favorites", label: "Favorite Documents", icon: Heart },
  { id: "hidden", label: "Hidden Documents", icon: Lock },
];

// Google Drive and Gmail on the main page: each opens its own panel, with a dot when it is connected.
type Source = "drive" | "gmail";

function SourceButton({ source, onOpen }: { source: Source; onOpen: () => void }) {
  const drive = useDriveStatus();
  const gmail = useGmailStatus();
  const status = (source === "drive" ? drive : gmail).data;
  const connected = Boolean(status?.configured && status.connected);
  const label = source === "drive" ? "Google Drive" : "Gmail";
  const Icon = source === "drive" ? Cloud : Mail;
  return (
    <button
      className="lv-button lx-source"
      type="button"
      onClick={onOpen}
      aria-label={`${label}${connected ? " (connected)" : ""}`}
      title={connected ? `${label}: connected as ${status?.email}` : `${label}: connect and import`}
    >
      <Icon size={16} />
      {label}
      {connected && <span className="lx-connected-dot" aria-hidden="true" />}
    </button>
  );
}

const LAYOUTS = ["grid", "list"] as const;
type Layout = (typeof LAYOUTS)[number];

function Workspace({ logoutUrl, settings }: { logoutUrl: string; settings: Settings }) {
  const [section, setSection] = useState<Section>(() =>
    remembered("lv-section", ["view", "favorites", "hidden"] as const, "view"),
  );
  const [layout, setLayout] = useState<Layout>(() => remembered("lv-layout", LAYOUTS, "grid"));
  const [uploading, setUploading] = useState(false);
  const [source, setSource] = useState<Source | null>(null);
  const uploads = useUploads();

  const chooseSection = (id: Section) => {
    setSection(id);
    remember("lv-section", id);
  };

  const chooseLayout = (id: Layout) => {
    setLayout(id);
    remember("lv-layout", id);
  };

  return (
    <main className="lv-page">
      <div className="lv-header-row">
        <Brand />
        <div className="lv-header-actions">
          <button
            className={`lv-button${layout === "grid" ? " lv-button-active" : ""}`}
            type="button"
            aria-label="Grid view"
            aria-pressed={layout === "grid"}
            onClick={() => chooseLayout("grid")}
          >
            <LayoutGrid size={18} />
          </button>
          <button
            className={`lv-button${layout === "list" ? " lv-button-active" : ""}`}
            type="button"
            aria-label="List view"
            aria-pressed={layout === "list"}
            onClick={() => chooseLayout("list")}
          >
            <List size={18} />
          </button>
          <button className="lv-button lv-button-primary lv-header-main" type="button" onClick={() => setUploading(true)}>
            <Upload size={17} />
            Upload Docs
            {uploads.progress && <span className="lv-busy-dot" aria-label="Uploading" />}
          </button>
          <a className="lv-button lv-sign-out" href={logoutUrl}>
            <LogOut size={16} />
            Sign out
          </a>
        </div>
      </div>

      <div className="lx-sources" aria-label="Import from">
        <SourceButton source="drive" onOpen={() => setSource("drive")} />
        <SourceButton source="gmail" onOpen={() => setSource("gmail")} />
      </div>

      <nav className="lv-nav" aria-label="Gallery section">
        {SECTIONS.map(({ id, label, icon: Icon }) => (
          <button
            className={section === id ? "lv-nav-item lv-nav-active" : "lv-nav-item"}
            type="button"
            key={id}
            aria-label={label}
            aria-current={section === id ? "page" : undefined}
            onClick={() => chooseSection(id)}
          >
            <Icon size={17} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      {section === "hidden" ? (
        <HiddenGate noun="documents">
          <Gallery key={section} section={section} layout={layout} settings={settings} />
        </HiddenGate>
      ) : (
        <Gallery key={section} section={section} layout={layout} settings={settings} />
      )}

      {uploading && <UploadDialog uploads={uploads} settings={settings} onClose={() => setUploading(false)} />}
      {source && (
        <Dialog title={source === "drive" ? "Google Drive" : "Gmail"} size="large" onClose={() => setSource(null)}>
          {source === "drive" ? <DrivePanel settings={settings} /> : <GmailPanel settings={settings} />}
        </Dialog>
      )}
      <footer className="lv-footer">Luna Lexia{settings.version && ` · v${settings.version}`}</footer>
    </main>
  );
}

export function App() {
  const { data: session, error, refetch } = useSession<Settings>();

  if (error) return <UnreachablePage onRetry={() => refetch()} />;
  if (!session) return null;
  if (session.status === "signed_out") return <SignInPage url={session.login_url} />;
  if (session.status === "forbidden") return <ForbiddenPage email={session.email} logoutUrl={session.logout_url} />;
  return <Workspace logoutUrl={session.logout_url} settings={session.settings} />;
}
