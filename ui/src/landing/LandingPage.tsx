import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";
import "./LandingPage.css";

// Temporary placeholder for HeroSection until fully implemented
const HeroSection = () => {
    const navigate = useNavigate();
    const heroRef = useRef<HTMLElement | null>(null);

    useEffect(() => {
        const heroEl = heroRef.current;
        if (!heroEl) return;
        const scrollContainer = heroEl.closest(".landing-page") as HTMLElement | null;

        const update = () => {
            const scrollTop = scrollContainer ? scrollContainer.scrollTop : window.scrollY;
            const heroTop = scrollContainer ? heroEl.offsetTop : heroEl.getBoundingClientRect().top + scrollTop;
            const local = Math.max(0, Math.min(scrollTop - heroTop, heroEl.offsetHeight));
            heroEl.style.setProperty("--parallax-bg", `${local * 0.14}px`);
            heroEl.style.setProperty("--parallax-fg", `${local * 0.28}px`);
        };

        update();

        const onScroll = () => update();
        if (scrollContainer) scrollContainer.addEventListener("scroll", onScroll, { passive: true });
        else window.addEventListener("scroll", onScroll, { passive: true });
        window.addEventListener("resize", onScroll);
        return () => {
            if (scrollContainer) scrollContainer.removeEventListener("scroll", onScroll);
            else window.removeEventListener("scroll", onScroll);
            window.removeEventListener("resize", onScroll);
        };
    }, []);
    return (
        <section className="landing-hero" ref={heroRef}>
            <div className="hero-bg" aria-hidden="true" />
            <div className="hero-center">
                <div className="hero-headline">
                    <h1 className="hero-title">
                        “Drop me the score. Say a few words. I’ll sing it for you.”
                    </h1>
                    <p className="hero-subtitle hero-subtitle-wide">
                        AI sight-singing from MusicXML, via chat. No DAW required.
                    </p>
                </div>
            </div>
            <div className="hero-footer">
                <div className="hero-actions">
                    <button className="btn-primary" onClick={() => navigate("/app")}>
                        Try SightSinger <ArrowRight size={20} />
                    </button>
                    <button
                        className="btn-secondary"
                        onClick={() => {
                            const el = document.getElementById("showcase-section");
                            el?.scrollIntoView({ behavior: "smooth" });
                        }}
                    >
                        See it in action
                    </button>
                </div>
            </div>
        </section>
    );
};

export default function LandingPage() {
    const navigate = useNavigate();

    useEffect(() => {
        document.body.classList.add("landing-active");
        return () => {
            document.body.classList.remove("landing-active");
        };
    }, []);

    return (
        <div className="landing-page">
            <nav className="landing-nav">
                <div className="brand">
                    <Sparkles className="brand-icon" />
                    <span>SightSinger.AI</span>
                </div>
                <div className="nav-links">
                    <button className="btn-ghost" onClick={() => navigate("/app")}>Open Studio</button>
                </div>
            </nav>

            <HeroSection />

            <section className="landing-section">
                <h2 className="section-title">Built for real rehearsal flow</h2>
                <div className="use-cases-grid">
                    <div className="use-case-card">
                        <h3>Indie Producers</h3>
                        <p>"Fast vocal proof"</p>
                        <p className="description">Hear the melody and lyrics instantly without building a DAW mockup.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Choir Leaders</h3>
                        <p>"Sight-reading made simple"</p>
                        <p className="description">Send clear SATB takes in minutes. No pianist or rehearsal recordings needed.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Composers</h3>
                        <p>"Harmony check"</p>
                        <p className="description">Listen to your choral writing fast and catch spacing issues early.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Arrangers</h3>
                        <p>"Phrase shaping"</p>
                        <p className="description">Try alternative phrasings and dynamics with plain English prompts.</p>
                    </div>
                </div>
            </section>

            <section id="showcase-section" className="landing-section alt-bg">
                <h2 className="section-title">Hear the score, not the piano roll</h2>
                <p className="section-subtitle">Natural language direction, instant takes.</p>

                <div className="showcase-mockup">
                    <div className="mockup-left">
                        <div className="mockup-score-header">
                            <span>Amazing Grace.xml</span>
                            <span className="badge">Soprano</span>
                        </div>
                        <div className="mockup-score-body">
                            <div className="staff-line"></div>
                            <div className="staff-line"></div>
                            <div className="staff-line"></div>
                            <div className="staff-line"></div>
                            <div className="staff-line"></div>
                            <div className="note-group">
                                <div className="note" style={{ left: '10%', top: '20px' }}></div>
                                <div className="note" style={{ left: '25%', top: '10px' }}></div>
                                <div className="note" style={{ left: '40%', top: '30px' }}></div>
                            </div>
                        </div>
                    </div>

                    <div className="mockup-right">
                        <div className="mockup-chat-bubble user">
                            Make the soprano lighter and breathy, like a soft entry.
                        </div>
                        <div className="mockup-chat-bubble assistant">
                            <Sparkles size={16} className="inline-icon" />
                            Got it. Here's a breathier soprano take from the same score.
                            <div className="mockup-audio">
                                <div className="play-btn">▶</div>
                                <div className="waveform">||||||||||||</div>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-section alt-bg">
                <h2 className="section-title">Voices built for sight-singing</h2>
                <div className="voice-gallery">
                    <div className="voice-card">
                        <div className="voice-avatar" style={{ background: 'linear-gradient(45deg, #ff9a9e 0%, #fecfef 99%, #fecfef 100%)' }}></div>
                        <h3>Raine Rena</h3>
                        <span className="tag">Soprano</span>
                        <p>Clear, bright, and perfect for pop and anime styles.</p>
                    </div>
                    <div className="voice-card">
                        <div className="voice-avatar" style={{ background: 'linear-gradient(120deg, #84fab0 0%, #8fd3f4 100%)' }}></div>
                        <h3>Solaria Tech</h3>
                        <span className="tag">Alto / Mezzo</span>
                        <p>Powerful, soulful, and rich. Ideal for ballads and jazz.</p>
                    </div>
                    <div className="voice-card">
                        <div className="voice-avatar" style={{ background: 'linear-gradient(to top, #cfd9df 0%, #e2ebf0 100%)' }}></div>
                        <h3>Atlas Prime</h3>
                        <span className="tag">Tenor</span>
                        <p>A classic male vocal with distinct clarity and range.</p>
                    </div>
                </div>
            </section>

            <section className="landing-section">
                <h2 className="section-title">Why SightSinger.AI?</h2>
                <p className="section-subtitle">Speak music, not MIDI.</p>

                <div className="comparison-container">
                    <table className="comparison-table">
                        <thead>
                            <tr>
                                <th>Feature</th>
                                <th className="highlight">SightSinger.AI</th>
                                <th>Traditional Tools</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td>Interface</td>
                                <td className="highlight">Natural Language (Chat)</td>
                                <td>Piano roll editing</td>
                            </tr>
                            <tr>
                                <td>Learning Curve</td>
                                <td className="highlight">Zero setup</td>
                                <td>Steep learning curve</td>
                            </tr>
                            <tr>
                                <td>Focus</td>
                                <td className="highlight">Sight-reading speed</td>
                                <td>Micromanaged note edits</td>
                            </tr>
                            <tr>
                                <td>Score Edits</td>
                                <td className="highlight">Zero-shot from score</td>
                                <td>Manual reprogramming</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </section>
            <section className="landing-section">
                <h2 className="section-title">How it works</h2>
                <div className="workflow-steps">
                    <div className="step-card">
                        <div className="step-number">01</div>
                        <h3>Upload your score</h3>
                        <p>Drop in MusicXML from MuseScore, Finale, or Sibelius.</p>
                    </div>
                    <div className="step-card">
                        <div className="step-number">02</div>
                        <h3>Direct the singer</h3>
                        <p>Use plain English to shape phrasing, tone, and expression.</p>
                    </div>
                    <div className="step-card">
                        <div className="step-number">03</div>
                        <h3>Generate a take</h3>
                        <p>Get a clean preview without building a DAW mockup.</p>
                    </div>
                    <div className="step-card">
                        <div className="step-number">04</div>
                        <h3>Refine and share</h3>
                        <p>Iterate fast and send it to your singers.</p>
                    </div>
                </div>
            </section>

            <footer className="landing-footer">
                <div className="footer-content">
                    <div className="footer-brand">
                        <Sparkles size={24} />
                        <span>SightSinger.AI</span>
                    </div>
                    <div className="footer-links">
                        <a href="#">GitHub</a>
                        <a href="#">API Docs</a>
                        <a href="#">About Us</a>
                    </div>
                </div>
                <p className="copyright">© 2026 SightSinger.AI. Powered by Gemini & DiffSinger.</p>
            </footer>
        </div>
    );
}
