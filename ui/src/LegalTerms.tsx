import { useEffect } from "react";
import "./LegalPage.css";

const APP_DOMAIN = "sightsinger.app";
const APP_BRAND_DOMAIN = "SightSinger.app";
const SUPPORT_EMAIL = `support@${APP_DOMAIN}`;

export default function LegalTerms() {
  useEffect(() => {
    document.body.classList.add("legal-active");
    return () => {
      document.body.classList.remove("legal-active");
    };
  }, []);

  return (
    <main className="legal-page">
      <h1>Terms of Service</h1>
      <p className="legal-updated">Last updated: 2026-01-23</p>

      <p>
        These Terms of Service ("Terms") govern your access to and use of
        {APP_BRAND_DOMAIN} (the "Service"). By accessing or using the Service, you
        agree to these Terms.
      </p>

      <h2>1. Eligibility</h2>
      <p>
        You must be at least 13 years old (or the minimum age required in your
        country) to use the Service. If you use the Service on behalf of an
        organisation, you represent that you have authority to bind that
        organisation.
      </p>

      <h2>2. Accounts and Access</h2>
      <p>
        You may sign in using supported methods (e.g., Google or email magic
        link). You are responsible for maintaining the confidentiality of your
        account access and for all activity under your account.
      </p>

      <h2>3. Trial Credits and Usage</h2>
      <p>
        We may grant trial credits that expire after a specified period.
        Credits represent access to Service usage and are not a currency, do
        not have cash value, and are not transferable. We may change trial
        terms or suspend trials at any time.
      </p>

      <h2>4. Paid Plans</h2>
      <p>
        If/when paid plans are offered, pricing, billing terms, and credit
        expiry will be described in the Service or at checkout. Additional
        terms may apply.
      </p>

      <h2>5. Acceptable Use</h2>
      <p>You agree not to:</p>
      <ul>
        <li>Use the Service for unlawful, harmful, or abusive activities.</li>
        <li>Infringe or misappropriate the rights of others.</li>
        <li>Attempt to interfere with or compromise the Service or its security.</li>
        <li>Circumvent usage limits or access controls.</li>
      </ul>

      <h2>6. Content and Rights</h2>
      <p>
        You retain rights to content you upload (such as MusicXML). You grant
        us a limited licence to process your content for the purpose of
        providing the Service. You are responsible for ensuring you have the
        rights to upload and process your content.
      </p>

      <h2>7. AI Output and Voicebanks</h2>
      <p>
        The Service generates audio based on your input and available
        voicebanks. Availability, quality, and licensing terms for voicebanks
        may vary. You are responsible for verifying the licensing and
        permitted usage of any generated audio.
      </p>

      <h2>8. Service Availability</h2>
      <p>
        We do not guarantee uninterrupted availability. The Service may be
        updated, changed, or discontinued at any time.
      </p>

      <h2>9. Disclaimers</h2>
      <p>
        The Service is provided "as is" and "as available" without warranties
        of any kind, including implied warranties of merchantability, fitness
        for a particular purpose, or non-infringement.
      </p>

      <h2>10. Limitation of Liability</h2>
      <p>
        To the fullest extent permitted by law, we are not liable for any
        indirect, incidental, special, consequential, or punitive damages, or
        for any loss of profits, data, or goodwill arising from your use of the
        Service.
      </p>

      <h2>11. Termination</h2>
      <p>
        We may suspend or terminate your access to the Service at any time for
        violation of these Terms or for any other reason. You may stop using
        the Service at any time.
      </p>

      <h2>12. Changes to These Terms</h2>
      <p>
        We may update these Terms from time to time. The "Last updated" date
        indicates the most recent changes. Continued use of the Service after
        changes become effective constitutes acceptance.
      </p>

      <h2>13. Governing Law</h2>
      <p>
        These Terms are governed by the laws of England and Wales, and you
        agree to the exclusive jurisdiction of the courts of England and
        Wales, unless mandatory local law provides otherwise.
      </p>

      <h2>14. Contact</h2>
      <p>If you have questions about these Terms, contact {SUPPORT_EMAIL}.</p>

      <p className="legal-disclaimer">
        This document is provided for general informational purposes and does
        not constitute legal advice.
      </p>
    </main>
  );
}
