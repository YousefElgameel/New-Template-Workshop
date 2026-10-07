// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Sources } from './Sources';

const DOCS = [
  { title: 'Observability architecture', path: 'docs/architecture/observability.md' },
  { title: 'HTTP API reference', path: 'docs/reference/http-api.md' },
];

describe('<Sources />', () => {
  afterEach(cleanup);

  it('shows each document by its title with its path beside it, in the order given', () => {
    render(<Sources citations={DOCS} />);
    const rows = within(screen.getByRole('list', { name: 'Sources' })).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Observability architecture');
    expect(rows[0]).toHaveTextContent('docs/architecture/observability.md');
    expect(within(rows[0]).getByText('docs/architecture/observability.md').tagName).toBe('CODE');
    expect(rows[1]).toHaveTextContent('HTTP API reference');
    expect(rows[1]).toHaveTextContent('docs/reference/http-api.md');
  });

  it('renders nothing when there are no citations', () => {
    const { container } = render(<Sources citations={[]} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('does not number the rows: the [n] in an answer are passage numbers, not list positions', () => {
    render(<Sources citations={DOCS} />);
    const block = screen.getByRole('list', { name: 'Sources' });
    expect(block.textContent).not.toMatch(/\[\d+\]|(^|\s)\d+\./);
    expect(block.querySelector('ol')).toBeNull();
  });

  it('treats titles and paths as plain text: no markup, no links, no link targets', () => {
    const hostile = [
      { title: '<img src=x onerror=alert(1)>', path: 'javascript:alert(1)' },
      { title: '[click](https://evil.example)', path: 'https://evil.example/steal?q=1' },
      { title: '<a href="https://evil.example">x</a>', path: 'data:text/html,<script>1</script>' },
    ];
    const { container } = render(<Sources citations={hostile} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('[href], [src], [onerror]')).toBeNull();
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(screen.getByText('javascript:alert(1)')).toBeInTheDocument();
    expect(screen.getByText('[click](https://evil.example)')).toBeInTheDocument();
    expect(screen.queryAllByRole('link')).toHaveLength(0);
  });

  it('keeps a long path and a long title from widening the message', () => {
    render(
      <Sources citations={[{ title: 'T'.repeat(300), path: 'docs/' + 'p'.repeat(300) + '.md' }]} />,
    );
    const row = screen.getByRole('listitem');
    expect(row).toHaveClass('min-w-0');
    expect(row.querySelector('code')).toHaveClass('break-all');
    expect(screen.getByRole('list', { name: 'Sources' })).toHaveClass('min-w-0');
  });

  it('accepts the same path twice without a key warning', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(<Sources citations={[DOCS[0], { ...DOCS[0], title: 'Same path, other title' }]} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });

  it('is a named list, once', () => {
    render(<Sources citations={DOCS} />);
    expect(screen.getAllByLabelText('Sources')).toHaveLength(1);
  });
});
