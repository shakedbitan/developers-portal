import React from 'react';
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import { restrictToParentElement } from '@dnd-kit/modifiers';
import {
  SortableContext,
  rectSortingStrategy,
  useSortable,
  arrayMove,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { SiteCard } from '../../components/SiteCard/SiteCard.jsx';
import { GroupedSiteCard } from '../../components/GroupedSiteCard/GroupedSiteCard.jsx';
import styles from './HomePage.module.css';

function SortableGroupCard({ item }) {
  const id = `group-${item.group_name}`;
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };
  return (
    <div ref={setNodeRef} style={style} {...attributes} {...listeners}>
      <GroupedSiteCard
        displayName={item.group_display_name}
        imageUrl={item.image_url}
        members={item.members}
        small
      />
    </div>
  );
}

function SortableSiteCard({ site, isStarred, onToggleStar, flipRef }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: site.id });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };

  // Attach both dnd-kit ref and FLIP ref to the same element
  const setRefs = (el) => {
    setNodeRef(el);
    if (flipRef) flipRef.current = el;
  };

  return (
    <div ref={setRefs} style={style} {...attributes} {...listeners}>
      <SiteCard
        site={site}
        isStarred={isStarred}
        onToggleStar={onToggleStar}
        isAdmin={false}
        onEdit={() => {}}
        onDelete={() => {}}
        small
      />
    </div>
  );
}

// Group starred sites by group_name. Sites without group_name stay individual.
function groupStarredSites(starredSites) {
  const groups = new Map();   // group_name → { meta, members[] }
  const solo   = [];

  for (const site of starredSites) {
    if (site.group_name) {
      if (!groups.has(site.group_name)) {
        groups.set(site.group_name, {
          group_name:         site.group_name,
          group_display_name: site.group_display_name || (site.group_name.charAt(0).toUpperCase() + site.group_name.slice(1)),
          image_url:          site.image_url,
          members:            [],
        });
      }
      const g = groups.get(site.group_name);
      // Use first non-null image in the group
      if (!g.image_url && site.image_url) g.image_url = site.image_url;
      g.members.push({
        id: site.id, url: site.url,
        env_label: site.env_label || site.name,
        image_url: site.image_url,
        env_color: site.env_color_hex,
      });
    } else {
      solo.push(site);
    }
  }

  // Return interleaved: groups and solos in original star order
  // Groups appear at position of their first member
  const result = [];
  const addedGroups = new Set();
  for (const site of starredSites) {
    if (site.group_name) {
      if (!addedGroups.has(site.group_name)) {
        addedGroups.add(site.group_name);
        result.push({ type: 'group', ...groups.get(site.group_name) });
      }
    } else {
      result.push({ type: 'solo', site });
    }
  }
  return result;
}

export function HomePage({ starredSites, starredIds, onToggleStar, onReorder, cardRefs }) {
  const sensors = useSensors(useSensor(PointerSensor, {
    activationConstraint: { distance: 8 },
    // Don't start drag when clicking env rows or links inside grouped cards
    onActivation: ({ event }) => {
      if (event.target.closest('[data-no-dnd="true"]')) return false;
    },
  }));

  const displayItems = groupStarredSites(starredSites);

  const lockScroll   = () => window.dispatchEvent(new CustomEvent('eden:dragStart'));
  const unlockScroll = () => window.dispatchEvent(new CustomEvent('eden:dragEnd'));

  const handleDragEnd = ({ active, over }) => {
    unlockScroll();
    // Suppress click that fires after drag by catching it at capture phase
    const suppressNextClick = (e) => { e.stopPropagation(); e.preventDefault(); };
    window.addEventListener('click', suppressNextClick, { capture: true, once: true });
    setTimeout(() => window.removeEventListener('click', suppressNextClick, true), 300);
    if (!over || active.id === over.id) return;

    const activeIdx = displayItems.findIndex(item =>
      item.type === 'group' ? `group-${item.group_name}` === active.id : item.site.id === active.id
    );
    const overIdx = displayItems.findIndex(item =>
      item.type === 'group' ? `group-${item.group_name}` === over.id : item.site.id === over.id
    );
    if (activeIdx < 0 || overIdx < 0) return;

    // BUG FIX: activeIdx/overIdx are positions in displayItems (grouped —
    // one entry per group, however many members it has), but starredSites
    // is the flat per-site list. Once any group exists, displayItems is
    // shorter than starredSites and every index past the group pointed at
    // the wrong site — that's what made dragging erratic and reorders not
    // stick. Reorder displayItems itself (each entry, group or solo, moves
    // as one unit), then flatten back to a per-site list, expanding each
    // group back into its members in their original relative order.
    const reorderedDisplay = arrayMove(displayItems, activeIdx, overIdx);
    const newFlatOrder = reorderedDisplay.flatMap(item =>
      item.type === 'group'
        ? starredSites.filter(s => s.group_name === item.group_name)
        : [item.site]
    );
    onReorder(newFlatOrder);
  };

  return (
    <div className={styles.page}>
      <div className={styles.spacer} />

      <div className={styles.bottom}>
        {starredSites.length > 0 ? (
          <>
            <div className={styles.label}>Your Apps</div>
            <DndContext sensors={sensors} collisionDetection={closestCenter}
                  modifiers={[restrictToParentElement]}
                  onDragStart={lockScroll} onDragEnd={handleDragEnd} onDragCancel={unlockScroll}>
              <SortableContext
                items={displayItems.map(item =>
                  item.type === 'group' ? `group-${item.group_name}` : item.site.id
                )}
                strategy={rectSortingStrategy}>
                <div className={styles.grid}>
                  {displayItems.map((item, idx) =>
                    item.type === 'group' ? (
                      // No extra wrapper div here — SortableGroupCard's own
                      // node must be a direct sibling of SortableSiteCard's
                      // nodes for dnd-kit's rectSortingStrategy to measure
                      // and position it correctly. An extra nesting level
                      // was throwing off both its row position and its
                      // ability to be dragged at all.
                      <SortableGroupCard key={`group-${item.group_name}`} item={item} />
                    ) : (
                      <SortableSiteCard
                        key={item.site.id}
                        site={item.site}
                        isStarred={starredIds.has(item.site.id)}
                        onToggleStar={onToggleStar}
                        flipRef={{
                          get current() { return cardRefs?.current?.[item.site.id]; },
                          set current(el) { if (cardRefs?.current) cardRefs.current[item.site.id] = el; }
                        }}
                      />
                    )
                  )}
                </div>
              </SortableContext>
            </DndContext>
          </>
        ) : (
          <div className={styles.empty}>
            Star apps from the Web Apps tab to see them here
          </div>
        )}
      </div>
    </div>
  );
}
