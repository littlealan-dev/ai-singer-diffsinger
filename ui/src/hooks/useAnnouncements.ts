import { useState, useEffect } from 'react';
import { doc, onSnapshot, updateDoc, serverTimestamp } from 'firebase/firestore';
import { db } from '../firebase';
import { useAuth } from './useAuth';
import { LATEST_ANNOUNCEMENT_ID, ANNOUNCEMENTS, Announcement } from '../announcements';

export interface UseAnnouncementsResult {
    showAnnouncement: boolean;
    currentAnnouncement: Announcement | undefined;
    markAsSeen: (announcementId: string) => Promise<void>;
    loading: boolean;
}

export function useAnnouncements(): UseAnnouncementsResult {
    const { user } = useAuth();
    const [lastSeenId, setLastSeenId] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        if (!user) {
            setLoading(false);
            return;
        }

        const userDocRef = doc(db, 'users', user.uid);
        const unsubscribe = onSnapshot(userDocRef, (snapshot) => {
            if (snapshot.exists()) {
                const data = snapshot.data();
                setLastSeenId(data.metadata?.lastSeenAnnouncementId || null);
            } else {
                setLastSeenId(null);
            }
            setLoading(false);
        }, (error) => {
            console.error("Error listening to user metadata:", error);
            setLoading(false);
        });

        return () => unsubscribe();
    }, [user]);

    const markAsSeen = async (announcementId: string) => {
        if (!user) return;
        const userDocRef = doc(db, 'users', user.uid);
        try {
            await updateDoc(userDocRef, {
                'metadata.lastSeenAnnouncementId': announcementId,
                'metadata.lastSeenAnnouncementDate': serverTimestamp()
            });
        } catch (error) {
            console.error("Error marking announcement as seen:", error);
        }
    };

    const isWithinEffectiveRange = (announcement: Announcement) => {
        const now = new Date();
        const fromDate = new Date(announcement.effectiveFrom);
        
        // Start of day for fromDate
        fromDate.setHours(0, 0, 0, 0);
        
        if (now < fromDate) return false;
        
        if (announcement.effectiveTo) {
            const toDate = new Date(announcement.effectiveTo);
            // End of day for toDate
            toDate.setHours(23, 59, 59, 999);
            if (now > toDate) return false;
        }
        
        return true;
    };

    const currentAnnouncement = ANNOUNCEMENTS.find(a => a.id === LATEST_ANNOUNCEMENT_ID);
    
    // Condition 1: Not loading
    // Condition 2: User is logged in
    // Condition 3: User hasn't seen this specific announcement yet
    // Condition 4: Current date is within the effective range
    const showAnnouncement = 
        !loading && 
        !!user && 
        lastSeenId !== LATEST_ANNOUNCEMENT_ID && 
        !!currentAnnouncement && 
        isWithinEffectiveRange(currentAnnouncement);

    return {
        showAnnouncement,
        currentAnnouncement,
        markAsSeen,
        loading
    };
}
