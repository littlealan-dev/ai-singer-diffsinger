import React from 'react';
import { useCredits } from '../hooks/useCredits';
import { Calendar, Flame, AlertCircle } from 'lucide-react';
import './CreditsHeader.css';

const CreditsHeader: React.FC = () => {
    const { available, expiresAt, isExpired, overdrafted, loading } = useCredits();

    if (loading) return <div className="credits-pill loading">...</div>;

    const daysLeft = expiresAt
        ? Math.ceil((expiresAt.getTime() - Date.now()) / (1000 * 60 * 60 * 24))
        : 0;

    let statusClass = 'normal';
    let icon = <Flame size={14} className="icon" />;
    let label = `${available} Credits`;

    if (overdrafted || isExpired || available <= 0) {
        statusClass = 'danger';
        icon = <AlertCircle size={14} className="icon" />;
        if (overdrafted) label = `${available} Credits (Overdraft)`;
        else if (isExpired) label = `${available} Credits (Expired)`;
        else label = '0 Credits';
    } else if (available <= 2) {
        statusClass = 'warning';
    }

    return (
        <div className={`credits-pill ${statusClass}`} title={isExpired ? "Credits have expired" : overdrafted ? "Account locked due to negative balance" : ""}>
            <div className="credits-main">
                {icon}
                <span className="credits-label">{label}</span>
            </div>
            {!overdrafted && !isExpired && (
                <div className="credits-divider" />
            )}
            {!overdrafted && !isExpired && (
                <div className="credits-expiry">
                    <Calendar size={12} className="icon-small" />
                    <span>{daysLeft}d left</span>
                </div>
            )}
        </div>
    );
};

export default CreditsHeader;
