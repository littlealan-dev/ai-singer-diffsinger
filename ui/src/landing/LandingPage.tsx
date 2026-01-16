import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Instagram, Mail, MessageCircle, MessageSquare, Sparkles } from "lucide-react";
import { AuthModal } from "../components/AuthModal";
import { UserMenu } from "../components/UserMenu";
import { useAuth } from "../hooks/useAuth.tsx";
import "./LandingPage.css";

// Hero section with parallax effect
interface HeroSectionProps {
    onStartTrial: () => void;
}

const HeroSection = ({ onStartTrial }: HeroSectionProps) => {
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
                        "Drop me the score. Say a few words. I'll sing it for you."
                    </h1>
                    <p className="hero-subtitle hero-subtitle-wide">
                        AI sight-singing from MusicXML, via chat. No DAW required.
                    </p>
                </div>
            </div>
            <div className="hero-footer">
                <div className="hero-actions">
                    <button
                        className="btn-primary btn-trial"
                        onClick={onStartTrial}
                    >
                        Start Free Trial <Sparkles size={18} />
                    </button>
                    <button
                        className="btn-secondary"
                        onClick={() => navigate("/demo")}
                    >
                        Try Interactive Demo <ArrowRight size={20} />
                    </button>
                </div>
            </div>
        </section>
    );
};


export default function LandingPage() {
    const navigate = useNavigate();
    const { isAuthenticated } = useAuth();
    const [openFaqIndex, setOpenFaqIndex] = useState<number | null>(0);
    const [showAuthModal, setShowAuthModal] = useState(false);

    useEffect(() => {
        document.body.classList.add("landing-active");
        return () => {
            document.body.classList.remove("landing-active");
        };
    }, []);

    const scrollToTop = () => {
        const container = document.querySelector(".landing-page");
        if (container) {
            container.scrollTo({ top: 0, behavior: "smooth" });
        }
    };

    const handleStartTrial = () => {
        if (isAuthenticated) {
            navigate("/app");
        } else {
            setShowAuthModal(true);
        }
    };

    return (
        <div className="landing-page">
            <nav className="landing-nav">
                <div className="brand" onClick={scrollToTop} style={{ cursor: 'pointer' }}>
                    <Sparkles className="brand-icon" />
                    <span>SightSinger.ai</span>
                </div>
                <div className="nav-links">
                    <button className="btn-nav-secondary" onClick={() => navigate("/demo")}>Try the Demo</button>
                    <button className="btn-nav-primary" onClick={handleStartTrial}>
                        {isAuthenticated ? "Go to Studio" : "Start Free Trial"}
                    </button>
                    {isAuthenticated && <UserMenu />}
                </div>
            </nav>

            <HeroSection onStartTrial={handleStartTrial} />

            <AuthModal
                isOpen={showAuthModal}
                onClose={() => setShowAuthModal(false)}
                onSuccess={() => navigate("/app")}
            />

            <section className="landing-section">
                <h2 className="section-title">Who is SightSinger.ai for?</h2>
                <div className="use-cases-grid">
                    <div className="use-case-card">
                        <h3>Indie Songwriters</h3>
                        <p>"Instant vocal demo"</p>
                        <p className="description">Get a convincing singing preview fast—no session singer, no studio time.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Choir &amp; Worship Leaders</h3>
                        <p>"Parts in minutes"</p>
                        <p className="description">Generate SATB (or melody) practice tracks in minutes—save pianist hours of practice and recording time.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Beginner Singers</h3>
                        <p>"Stepping stone"</p>
                        <p className="description">Try singing with a score-accurate guide before investing in expensive lessons.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>Quick Song Learners</h3>
                        <p>"Learn it fast"</p>
                        <p className="description">Pick up a few songs quickly for occasions without diving into theory or breath training.</p>
                    </div>
                </div>
            </section>

            <section className="landing-section">
                <h2 className="section-title">Why SightSinger.ai?</h2>
                <p className="section-subtitle">Speak music, not MIDI.</p>

                <div className="comparison-container">
                    <table className="comparison-table">
                        <thead>
                            <tr>
                                <th>Feature</th>
                                <th className="highlight">SightSinger.ai</th>
                                <th>Professional DAW (Digital Audio Workstation)</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td>Interface</td>
                                <td className="highlight">Natural Language (Chat)</td>
                                <td>Piano roll, parameters, phonemes</td>
                            </tr>
                            <tr>
                                <td>Learning Curve</td>
                                <td className="highlight">Zero</td>
                                <td>Steep</td>
                            </tr>
                            <tr>
                                <td>Focus</td>
                                <td className="highlight">Global style control, demo quality</td>
                                <td>Detailed note-level control, production quality</td>
                            </tr>
                            <tr>
                                <td>Time to result</td>
                                <td className="highlight">Minutes</td>
                                <td>Hours</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </section>

            <section className="landing-section alt-bg">
                <h2 className="section-title">SightSinger.ai is <i>NOT</i>...</h2>
                <div className="use-cases-grid">
                    <div className="use-case-card">
                        <h3>A DAW replacement</h3>
                        <p className="description">It doesn’t replace professional vocal synthesis tools. But it can be used to complement them with fast, score-based vocal previews.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>A human vocalist</h3>
                        <p className="description">It’s a fast, score-accurate singing demo for rehearsal and practice. It doesn't replace human singers or human recordings.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>A Song Generator</h3>
                        <p className="description">It doesn’t generate songs from text prompts (like Suno). It sings the music you’ve already written and follows the score exactly.</p>
                    </div>
                    <div className="use-case-card">
                        <h3>A Voice Converter</h3>
                        <p className="description">It doesn’t convert recorded vocals or require a base audio track. It sings directly from the score.</p>
                    </div>
                </div>
            </section>
            <section className="landing-section compact">
                <h2 className="section-title">How it works</h2>
                <div className="timeline">
                    <div className="timeline-row">
                        <div className="timeline-step">01</div>
                        <div className="timeline-content">
                            <h3>Upload your score</h3>
                            <p>Drop in MusicXML from MuseScore, Logic Pro, Finale, or Sibelius. <br />SightSinger.ai parses tempo, parts, and lyrics.</p>
                        </div>
                    </div>
                    <div className="timeline-row">
                        <div className="timeline-step">02</div>
                        <div className="timeline-content">
                            <h3>Tell the singer</h3>
                            <p>Use natural language to pick parts/verses and shape phrasing, tone, and expression. <br />The AI translates your intent into performance instructions.</p>
                        </div>
                    </div>
                    <div className="timeline-row">
                        <div className="timeline-step">03</div>
                        <div className="timeline-content">
                            <h3>Generate the singing voice</h3>
                            <p>SightSinger.ai uses DiffSinger, an open-source singing voice synthesis model, to generate a realistic vocal demo directly from the score. <br />Voicebanks are OpenUtau-compatible ONNX format, a widely supported format that allows easy voice bank expansion.</p>
                        </div>
                    </div>
                    <div className="timeline-row">
                        <div className="timeline-step">04</div>
                        <div className="timeline-content">
                            <h3>Refine and share</h3>
                            <p>Chat to iterate quickly, render new takes, and share the demo with singers.</p>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-section">
                <h2 className="section-title">AI Voices</h2>
                <p className="section-subtitle voicebanks-subtitle">
                    All AI voices in SightSinger.ai are <span className="highlight-text">royalty-free</span>. More AI voices are coming soon.
                </p>
                <div className="voicebanks-grid">
                    <div className="voicebank-card">
                        <img
                            src="/voicebanks/reizo_icon.png"
                            alt="Raine Reizo icon"
                            className="voicebank-icon"
                            loading="lazy"
                        />
                        <div className="voicebank-body">
                            <h3>Raine Reizo</h3>
                            <p className="voicebank-profile">
                                Version 2.0 · by suyu (UtauReizo) · 3 tone colors (normal/soft/strong)
                            </p>
                        </div>
                    </div>
                    <div className="voicebank-card">
                        <img
                            src="/voicebanks/rena_icon.png"
                            alt="Raine Rena icon"
                            className="voicebank-icon"
                            loading="lazy"
                        />
                        <div className="voicebank-body">
                            <h3>Raine Rena</h3>
                            <p className="voicebank-profile">
                                Version 2.0 · by suyu (UtauReizo) · 3 tone colors (normal/soft/strong)
                            </p>
                        </div>
                    </div>
                </div>
                <div className="voicebank-credits">
                    <div className="voicebank-credits-title">CREDITS</div>
                    <div>Website: <a href="https://rainerr.weebly.com/" target="_blank" rel="noreferrer">https://rainerr.weebly.com/</a></div>
                    <div>Voiced, labelled &amp; trained by suyu (UtauReizo)</div>
                    <div className="voicebank-credits-subtitle">Datasets used for parallel training:</div>
                    <ul>
                        <li>Raine Rena</li>
                        <li>Fromage</li>
                        <li>PJS (relabel by UtaUtaUtau)</li>
                        <li>CSD (relabel by heta-tan)</li>
                        <li>opencpop (relabel by komisteng)</li>
                        <li>m4singer (relabel by nobodyP)</li>
                        <li>TGM</li>
                    </ul>
                </div>
            </section>

            <section className="landing-section alt-bg">
                <h2 className="section-title">FAQ</h2>
                <div className="faq-list">
                    {[
                        {
                            key: "alternatives",
                            q: "Why not use ACE Studio, Cantai, or OpenUtau?",
                            a: "Those tools are built for detailed vocal production and require note-by-note or phoneme editing. SightSinger.ai generates quick song previews using natural language directions, without DAW or MIDI knowledge.",
                        },
                        {
                            key: "formats",
                            q: "What file format do you support?",
                            a: "MusicXML (.xml or compressed .mxl) from any MuseScore, Logic Pro, Finale, or Sibelius.",
                        },
                        {
                            key: "pdf",
                            q: "Can I upload a PDF score instead of MusicXML?",
                            a: (
                                <>
                                    Not yet. I'd love to support PDF-to-singing in the future, but turning PDFs into
                                    accurate MusicXML is already a complex problem and takes time to get right. Also,
                                    there are already good online tools by MuseScore and ACE Studio that convert PDF
                                    scores to MusicXML.
                                    <br />
                                    You can use those to convert PDF to MusicXML first, then upload the MusicXML file
                                    here to hear your song.
                                </>
                            ),
                        },
                        {
                            key: "royalty",
                            q: "Are the AI voices in SightSinger.ai royalty-free for music production?",
                            a: "Yes, all the AI voices in SightSinger.ai are royalty-free for commercial projects. You can publish, distribute, or monetize your music without paying extra fees.",
                        },
                    ].map((item, index) => {
                        const isOpen = openFaqIndex === index;
                        return (
                            <div className={`faq-item ${isOpen ? "open" : ""}`} key={item.key}>
                                <button
                                    className="faq-question"
                                    type="button"
                                    onClick={() => setOpenFaqIndex(isOpen ? null : index)}
                                >
                                    <span>{item.q}</span>
                                    <span className="faq-toggle" aria-hidden="true">
                                        {isOpen ? "–" : "+"}
                                    </span>
                                </button>
                                {isOpen && <div className="faq-answer">{item.a}</div>}
                            </div>
                        );
                    })}
                </div>
            </section>

            <section id="about-section" className="landing-section">
                <h2 className="section-title">About Me</h2>
                <div className="about-content">
                    <div className="bio-list">
                        <p className="bio-paragraph">
                            I'm Alan, a software engineer and volunteer musician. I love building things that solve real problems. I'm passionate about using AI to solve my own problem in music, and hopefully it can help you too.
                        </p>
                        <div className="bio-item">
                            <span className="bio-label">Background</span>
                            <span className="bio-value">
                                <span>Software engineering: 20+ years (full-stack)</span>
                                <span>Music Instruments: Piano, drums, ukulele</span>
                                <span>Active in a church choir and community concert band</span>
                                <span>Music tech: Logic Pro</span>
                                <span>AI singing / vocal synthesis: OpenUtau, DiffSinger</span>
                            </span>
                        </div>
                        <div className="bio-socials" aria-label="Social links">
                            <a
                                className="bio-social"
                                href="https://www.instagram.com/twittlealan/"
                                target="_blank"
                                rel="noreferrer"
                                aria-label="Instagram"
                            >
                                <Instagram size={18} />
                            </a>
                            <a
                                className="bio-social"
                                href="https://discord.com/users/littlealan1915"
                                target="_blank"
                                rel="noreferrer"
                                aria-label="Discord"
                            >
                                <MessageCircle size={18} />
                            </a>
                            <a
                                className="bio-social"
                                href="https://www.reddit.com/user/littleAlanYT/"
                                target="_blank"
                                rel="noreferrer"
                                aria-label="Reddit"
                            >
                                <MessageSquare size={18} />
                            </a>
                            <a
                                className="bio-social"
                                href="mailto:littlealan@gmail.com"
                                aria-label="Email"
                            >
                                <Mail size={18} />
                            </a>
                        </div>
                    </div>
                </div>
            </section>

            <footer className="landing-footer">
                <div className="footer-content">
                    <div className="footer-brand">
                        <Sparkles size={24} />
                        <span>SightSinger.ai</span>
                    </div>
                    <div className="footer-links">
                        <a href="https://github.com/littlealan-dev/ai-singer-diffsinger" target="_blank" rel="noreferrer">GitHub</a>
                        <a href="#about-section">About Me</a>
                    </div>
                </div>
                <p className="copyright">© 2026 SightSinger.ai.</p>
            </footer>
        </div>
    );
}
