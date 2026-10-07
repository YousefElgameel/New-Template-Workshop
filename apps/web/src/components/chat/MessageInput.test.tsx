// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageInput } from './MessageInput';

describe('<MessageInput />', () => {
  afterEach(cleanup); // Vitest globals are off, so RTL does not auto-clean between tests

  it('sends trimmed text on Enter and clears; Shift+Enter inserts a newline', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<MessageInput onSubmit={onSubmit} />);
    const box = screen.getByLabelText('Message');

    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    await user.type(box, '  hello  ');
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
    await user.keyboard('{Enter}');
    expect(onSubmit).toHaveBeenCalledWith('hello');
    expect(box).toHaveValue('');

    await user.type(box, 'a{Shift>}{Enter}{/Shift}b');
    expect(box).toHaveValue('a\nb');
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('is inert while disabled', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<MessageInput onSubmit={onSubmit} disabled />);
    const box = screen.getByLabelText('Message');
    expect(box).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  describe('with a character limit', () => {
    it('shows a counter and sends up to the limit', async () => {
      const user = userEvent.setup();
      const onSubmit = vi.fn();
      render(<MessageInput onSubmit={onSubmit} maxChars={5} />);
      expect(screen.getByText('0 / 5')).toBeInTheDocument();
      await user.type(screen.getByLabelText('Message'), 'abcde');
      expect(screen.getByText('5 / 5')).toBeInTheDocument();
      await user.keyboard('{Enter}');
      expect(onSubmit).toHaveBeenCalledWith('abcde');
    });

    it('refuses one more than the limit, by button and by Enter, and says so', async () => {
      const user = userEvent.setup();
      const onSubmit = vi.fn();
      render(<MessageInput onSubmit={onSubmit} maxChars={5} />);
      const box = screen.getByLabelText('Message');
      await user.type(box, 'abcdef');
      expect(screen.getByText(/6 \/ 5/)).toHaveTextContent('too long');
      expect(box).toHaveAttribute('aria-invalid', 'true');
      expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
      await user.keyboard('{Enter}');
      expect(onSubmit).not.toHaveBeenCalled();
      expect(box).toHaveValue('abcdef'); // nothing is lost
      await user.keyboard('{Backspace}');
      expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
    });

    it('counts code points, not UTF-16 units, and ignores surrounding whitespace', async () => {
      const user = userEvent.setup();
      render(<MessageInput onSubmit={vi.fn()} maxChars={3} />);
      const box = screen.getByLabelText('Message');
      await user.click(box);
      await user.paste('  ' + String.fromCodePoint(0x1f600).repeat(3) + '  ');
      expect(screen.getByText('3 / 3')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
    });

    it('formats a large limit with a thousands separator, and shows no counter without one', () => {
      render(<MessageInput onSubmit={vi.fn()} maxChars={4000} />);
      expect(screen.getByText('0 / 4,000')).toBeInTheDocument();
      cleanup();
      render(<MessageInput onSubmit={vi.fn()} />);
      expect(screen.queryByText(/\//)).not.toBeInTheDocument();
    });
  });
});
