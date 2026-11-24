import { ChevronDown, Cloud, Eye, Heart, LayoutGrid, List, Lock, LogOut, Mail, Upload, type LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

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

// Upload Docs as a split button: the main part uploads from this device, the arrow opens Google Drive and Gmail,
// each saying whether it is connected.
type Source = "drive" | "gmail";

function UploadMenu({ busy, onUpload, onSource }: { busy: boolean; onUpload: () => void; onSource: (source: Source) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const drive = useDriveStatus();
  const gmail = useGmailStatus();

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.document.addEventListener("pointerdown", closeOutside);
    window.document.addEventListener("keydown", closeOnEscape);
    return () => {
      window.document.removeEventListener("pointerdown", closeOutside);
      window.document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const items: { source: Source; label: string; icon: LucideIcon; status: typeof drive.data }[] = [
    { source: "drive", label: "Google Drive", icon: Cloud, status: drive.data },
    { source: "gmail", label: "Gmail", icon: Mail, status: gmail.data },
  ];
  const choose = (source: Source) => {
    setOpen(false);
    onSource(source);
  };

  return (
    <div className="lx-split lv-header-main" ref={ref}>
      <button className="lv-button lv-button-primary lx-split-main" type="button" onClick={onUpload}>
        <Upload size={17} />
        Upload Docs
        {busy && <span className="lv-busy-dot" aria-label="Uploading" />}
      </button>
      <button
        className="lv-button lv-button-primary lx-split-arrow"
        type="button"
        aria-label="Import from Google Drive or Gmail"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <ChevronDown size={16} />
      </button>
      {open && (
        <div className="lx-split-menu" role="menu">
          {items.map(({ source, label, icon: Icon, status }) => {
            const connected = Boolean(status?.configured && status.connected);
            return (
              <button className="lx-split-item" type="button" role="menuitem" key={source} onClick={() => choose(source)}>
                <Icon size={16} />
                <span className="lx-split-label">
                  {label}
                  <small>{connected ? status?.email : status && !status.configured ? "Not set up" : "Not connected"}</small>
                </span>
                {connected && <span className="lx-connected-dot" aria-label="Connected" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
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
          <UploadMenu busy={Boolean(uploads.progress)} onUpload={() => setUploading(true)} onSource={setSource} />
          <a className="lv-button lv-sign-out" href={logoutUrl}>
            <LogOut size={16} />
            Sign out
          </a>
        </div>
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
