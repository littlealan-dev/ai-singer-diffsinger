import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";
import "./LandingPage.css";

// Temporary placeholder for HeroSection until fully implemented
const HeroSection = () => {
    const navigate = useNavigate();
    return (
        <section className="landing-hero">
            <div className="hero-content">
                <h1 className="hero-title">
                    Singing Voices, <span className="text-gradient">Reimagined</span>.
                </h1>
                <p className="hero-subtitle">
                    Transform scores with Gemini intelligence. Perform them with DiffSinger precision.
                </p>
                <div className="hero-actions">
                    <button className="btn-primary" onClick={() => navigate("/app")}>
                        Try the Demo <ArrowRight size={20} />
                    </button>
                    <button
                        className="btn-secondary"
                        onClick={() => {
                            const el = document.getElementById('showcase-section');
                            el?.scrollIntoView({ behavior: 'smooth' });
                        }}
                    >
                        Watch how it works
                    </button>
                </div>
            </div>
            <div className="hero-visual">
                {/* Placeholder for 3D animation */}
                <div className="visual-placeholder"></div>
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
                    <span>AI Singer</span>
                </div>
                <div className="nav-links">
                    <button className="btn-ghost" onClick={() => navigate("/app")}>Login</button>
                </div>
            </nav>

            <HeroSection />

            <section className="landing-section">
                <h2 className="section-title">Who is this for?</h2>
                <div className="use-cases-grid">
                    <div className="use-case-card">
                        <h3>Individual Producers</h3>
                        <p>"The 5-Minute Demo"</p>
                        <p className="description">Create high-quality vocal previews for your songs before hiring professional singers.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Choir Leaders</h3>
                        <p>"Effortless Rehearsal"</p>
                        <p className="description">Send accurate SATB singing demos to your choir in minutes. No pianist required.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Composers</h3>
                        <p>"Harmonic Sandbox"</p>
                        <p className="description">Hear your complex SATB harmonies instantly. Verify vocal flow before publishing.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Lyricists</h3>
                        <p>"Flow Check"</p>
                        <p className="description">Check prosody and rhythm naturally without needing to sing it yourself.</p>
                    </div>
                </div>
            </section>

            <section id="showcase-section" className="landing-section alt-bg">
                <h2 className="section-title">Experience the Magic</h2>
                <p className="section-subtitle">Real-time collaboration with Gemini.</p>

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
                            Make the soprano part more airy and soft, like a whisper.
                        </div>
                        <div className="mockup-chat-bubble assistant">
                            <Sparkles size={16} className="inline-icon" />
                            I've adjusted the breathiness parameter for the Soprano part. Here is a preview.
                            <div className="mockup-audio">
                                <div className="play-btn">▶</div>
                                <div className="waveform">||||||||||||</div>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-section alt-bg">
                <h2 className="section-title">Premium Voicebanks</h2>
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
                <h2 className="section-title">Why AI Singer?</h2>
                <p className="section-subtitle">Speak Music, Not MIDI.</p>

                <div className="comparison-container">
                    <table className="comparison-table">
                        <thead>
                            <tr>
                                <th>Feature</th>
                                <th className="highlight">The AI Singer</th>
                                <th>Traditional Tools</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td>Interface</td>
                                <td className="highlight">Natural Language (Chat)</td>
                                <td>Complicated MIDI Editor</td>
                            </tr>
                            <tr>
                                <td>Learning Curve</td>
                                <td className="highlight">Zero (Talk to Gemini)</td>
                                <td>Steep (Weeks of practice)</td>
                            </tr>
                            <tr>
                                <td>Focus</td>
                                <td className="highlight">Speed & Flow</td>
                                <td>Surgical Note Control</td>
                            </tr>
                            <tr>
                                <td>Score Edits</td>
                                <td className="highlight">Automatic (via Gemini)</td>
                                <td>Manual Point & Click</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </section>
            <section className="landing-section">
                <h2 className="section-title">How it Works</h2>
                <div className="workflow-steps">
                    <div className="step-card">
                        <div className="step-number">01</div>
                        <h3>Compose / Import</h3>
                        <p>Upload any MusicXML file from MuseScore, finale, or Sibelius.</p>
                    </div>
                    <div className="step-card">
                        <div className="step-number">02</div>
                        <h3>Instruct</h3>
                        <p>Tell Gemini how you want the performance to feel (e.g., "Make the soprano airy").</p>
                    </div>
                    <div className="step-card">
                        <div className="step-number">03</div>
                        <h3>Synthesize</h3>
                        <p>One-click high-fidelity synthesis via DiffSinger.</p>
                    </div>
                    <div className="step-card">
                        <div className="step-number">04</div>
                        <h3>Refine</h3>
                        <p>Tweak intensity and clarity in real-time until it's perfect.</p>
                    </div>
                </div>
            </section>

            <footer className="landing-footer">
                <div className="footer-content">
                    <div className="footer-brand">
                        <Sparkles size={24} />
                        <span>AI Singer</span>
                    </div>
                    <div className="footer-links">
                        <a href="#">GitHub</a>
                        <a href="#">API Docs</a>
                        <a href="#">About Us</a>
                    </div>
                </div>
                <p className="copyright">© 2026 AI Singer Project. Powered by Gemini & DiffSinger.</p>
            </footer>
        </div>
    );
}
