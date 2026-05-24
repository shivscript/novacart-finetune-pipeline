import { HttpClient } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { environment } from '../environments/environment';

type Msg = { role: 'user' | 'bot'; text: string };

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  private http = inject(HttpClient);
  private apiUrl = environment.apiUrl;

  messages = signal<Msg[]>([]);
  input = signal('');
  loading = signal(false);

  send() {
    const text = this.input().trim();
    if (!text || this.loading()) return;

    this.messages.update(m => [...m, { role: 'user', text }]);
    this.input.set('');
    this.loading.set(true);

    this.http.post<{ reply: string }>(this.apiUrl, { message: text }).subscribe({
      next: (res) => {
        this.messages.update(m => [...m, { role: 'bot', text: res.reply }]);
        this.loading.set(false);
      },
      error: (err) => {
        this.messages.update(m => [...m, { role: 'bot', text: 'Error: ' + (err.message || 'request failed') }]);
        this.loading.set(false);
      }
    });
  }
}