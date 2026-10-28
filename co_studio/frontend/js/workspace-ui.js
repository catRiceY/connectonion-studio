// Small DOM helpers shared by Studio's management pages. All data stays text.
export function node(doc, tag, className = '', text = '') {
  const element = doc.createElement(tag);
  if (className) element.className = className;
  if (text !== '') element.textContent = String(text);
  return element;
}

export function message(element, text = '', error = false) {
  element.textContent = text;
  element.hidden = !text;
  element.classList.toggle('is-error', error);
}

export function epochMillis(value) {
  return typeof value === 'number' ? value * 1000 : Date.parse(value);
}

export function timestamp(value) {
  const date = new Date(epochMillis(value));
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Not recorded';
}

export function populateAgents(select, agents, selected) {
  select.replaceChildren();
  for (const agent of agents) {
    const option = node(select.ownerDocument, 'option', '', agent.name || agent.slug);
    option.value = agent.slug; select.append(option);
  }
  select.disabled = !agents.length;
  select.value = agents.some((agent) => agent.slug === selected) ? selected : (agents[0]?.slug || '');
  return select.value;
}
