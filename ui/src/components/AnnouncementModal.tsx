import React, { useEffect, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { X, Bell, Sparkles } from 'lucide-react';
import confetti from 'canvas-confetti';
import './AnnouncementModal.css';
import { Announcement } from '../announcements';

interface AnnouncementModalProps {
    announcement: Announcement;
    onClose: () => void;
}

const AnnouncementModal: React.FC<AnnouncementModalProps> = ({ announcement, onClose }) => {
    const confettiExecuted = useRef(false);

    useEffect(() => {
        if (announcement.effect === 'fireworks' && !confettiExecuted.current) {
            confettiExecuted.current = true;
            
            // Standard burst
            confetti({
                particleCount: 150,
                spread: 70,
                origin: { y: 0.6 },
                colors: ['#b77dff', '#9b86ff', '#7c3aff', '#e2c7ff', '#ffffff']
            });

            // Realistic fireworks burst over 2 seconds
            const duration = 2 * 1000;
            const animationEnd = Date.now() + duration;
            const defaults = { startVelocity: 30, spread: 360, ticks: 60, zIndex: 9999 };

            const randomInRange = (min: number, max: number) => Math.random() * (max - min) + min;

            const interval: any = setInterval(function() {
                const timeLeft = animationEnd - Date.now();

                if (timeLeft <= 0) {
                    return clearInterval(interval);
                }

                const particleCount = 50 * (timeLeft / duration);
                // since particles fall down, start a bit higher than random
                confetti({
                    ...defaults,
                    particleCount,
                    origin: { x: randomInRange(0.1, 0.3), y: Math.random() - 0.2 }
                });
                confetti({
                    ...defaults,
                    particleCount,
                    origin: { x: randomInRange(0.7, 0.9), y: Math.random() - 0.2 }
                });
            }, 250);
        }
    }, [announcement.effect]);

    return (
        <div className="announcement-overlay">
            <div className="announcement-modal">
                <button className="announcement-close" onClick={onClose} aria-label="Close">
                    <X size={20} />
                </button>
                
                <div className="announcement-header">
                    <div className="announcement-icon-ring">
                        <Bell className="announcement-icon" />
                        <Sparkles className="announcement-sparkle" />
                    </div>
                </div>

                <div className="announcement-content">
                    <h2>{announcement.title}</h2>
                    <div className="announcement-body">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {announcement.content}
                        </ReactMarkdown>
                    </div>
                    <button className="announcement-button" onClick={onClose}>
                        Awesome, let's go!
                    </button>
                </div>
            </div>
        </div>
    );
};

export default AnnouncementModal;
