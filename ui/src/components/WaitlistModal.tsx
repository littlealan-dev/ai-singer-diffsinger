import { X } from "lucide-react";
import { WaitingListForm, type WaitlistSource } from "./WaitingListForm";
import "./WaitlistModal.css";

interface WaitlistModalProps {
  isOpen: boolean;
  onClose: () => void;
  source: WaitlistSource;
  title?: string;
  subtitle?: string;
}

export function WaitlistModal({
  isOpen,
  onClose,
  source,
  title = "Get SightSinger Updates",
  subtitle = "Receive product updates, feature news, and occasional announcements.",
}: WaitlistModalProps) {
  if (!isOpen) return null;
  return (
    <div className="waitlist-modal-overlay" onClick={onClose}>
      <div className="waitlist-modal" onClick={(event) => event.stopPropagation()}>
        <button className="waitlist-modal-close" onClick={onClose} aria-label="Close">
          <X size={18} />
        </button>
        <div className="waitlist-modal-header">
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        <WaitingListForm source={source} />
      </div>
    </div>
  );
}
