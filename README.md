# 📰 Scout - News Monitor

A powerful real-time news monitoring and analysis system that uses AI to track, cluster, and alert on breaking news events. Built with Python, Flask, and powered by local LLMs via Ollama.

![News Monitor Dashboard](https://img.shields.io/badge/Status-Ready%20for%20Production-green) 
![Python](https://img.shields.io/badge/Python-3.8+-blue) 
![Flask](https://img.shields.io/badge/Flask-2.3+-red) 
![Ollama](https://img.shields.io/badge/Ollama-Compatible-purple)

## ✨ Features

### 🔍 **Smart News Discovery**
- Real-time Google News RSS feed monitoring
- Custom search topics with 24-hour lookback
- Automatic story deduplication and clustering

### 🤖 **AI-Powered Analysis**
- Local LLM integration via Ollama (privacy-first)
- Intelligent event extraction and categorization
- Context-aware story summarization
- Status detection (ongoing, announced, escalating)

### 📊 **Advanced Event Tracking**
- Multi-story event clustering with similarity scoring
- Freshness, specificity, and impact scoring
- Cross-source corroboration validation
- Alert cooldown system to prevent spam

### 🌐 **Modern Web Interface**
- Beautiful, responsive Flask web application
- Real-time search progress with live updates
- Interactive dashboards for event monitoring
- Historical data browser and export functionality

### 🚨 **Intelligent Alerting**
- Configurable scoring thresholds
- High-priority event highlighting
- Timeline tracking with persistent storage
- Status-based alert triggers

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- [Ollama](https://ollama.com/) installed and running
- Basic familiarity with command line

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/news-monitor.git
   cd news-monitor
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up templates**
   ```bash
   python template_setup.py
   ```

4. **Configure environment**
   ```bash
   cp .env.template .env
   # Edit .env with your settings
   ```

5. **Start Ollama and pull a model**
   ```bash
   ollama serve
   ollama pull gemma2:2b  # or your preferred model
   ```

6. **Run the application**
   ```bash
   python app.py
   ```

7. **Open your browser**
   ```
   http://localhost:5000
   ```

## 📁 Project Structure

```
news-monitor/
├── 📄 app.py                 # Main Flask web application
├── 🧠 llama_utils.py         # Ollama/LLM integration utilities
├── 📰 main.py               # Core news processing logic
├── 📊 tracker.py            # Event clustering and tracking system
├── ⚙️  template_setup.py     # Template installation script
├── 📋 requirements.txt      # Python dependencies
├── 🌍 .env.template         # Environment configuration template
├── 📝 prompts/
│   └── news_prepper.txt     # LLM prompt for news analysis
├── 🎨 templates/            # Flask HTML templates
│   ├── base.html           # Main layout template
│   ├── index.html          # Homepage with search
│   ├── results.html        # Search results display
│   ├── historical.html     # Historical data browser
│   ├── event_store.html    # Event tracking dashboard
│   └── error.html          # Error pages
├── 💾 logs/                # Application logs
└── 📊 event_store.jsonl    # Persistent event tracking data
```

## 🎯 Usage Examples

### Basic News Monitoring
1. Enter a search topic: `"climate protests"`
2. Watch real-time progress as stories are analyzed
3. Review clustered events with AI-generated summaries
4. Monitor alerts for high-priority developments

### Advanced Event Tracking
```python
# Customize scoring parameters
tracker = EventTracker(
    cooldown_minutes=30,    # Time between alerts for same event
    min_score=60           # Minimum score for high-priority alerts
)
```

### API Integration
```bash
# Get system status
curl http://localhost:5000/api/status

# Export topic data
curl http://localhost:5000/api/export/climate_protests

# Get cluster details
curl http://localhost:5000/api/cluster/ice-enforcement-chicago
```

## ⚙️ Configuration

### Environment Variables
```bash
# Required: Ollama host (without http://)
URL=localhost

# Optional: ComfyUI integration (for future image analysis)
COMFY=localhost

# Flask settings
FLASK_ENV=production
FLASK_DEBUG=False

# Model configuration
DEFAULT_MODEL=gemma2:2b
```

### Customizing Event Scoring

The system scores events on multiple dimensions:

- **Freshness** (0-40): How recent the story is
- **Specificity** (0-30): Concrete locations, actors, actions
- **Corroboration** (0-20): Multiple credible sources
- **Impact** (0-10): Public safety implications

Edit `tracker.py` to adjust scoring weights for your use case.

### LLM Models

Compatible with any Ollama model. Recommended options:

| Model | Size | Speed | Quality | Best For |
|-------|------|-------|---------|----------|
| `gemma2:2b` | 1.6GB | ⚡ Fast | Good | Development/Testing |
| `llama3.1:8b` | 4.7GB | 🔄 Medium | Better | Production |
| `qwen2.5:14b` | 8.7GB | 🐌 Slow | Best | High Accuracy |

## 🔧 Advanced Features

### Historical Data Analysis
- Browse all previously monitored topics
- Export complete datasets as JSON
- Timeline analysis of event evolution
- Cross-topic pattern recognition

### Real-time Monitoring
- WebSocket-based live updates
- Configurable refresh intervals
- Background processing with progress tracking
- Multi-topic concurrent monitoring

### Integration Ready
- RESTful API endpoints
- JSON export functionality  
- Webhook support (coming soon)
- Database integration options

## 📈 Monitoring Dashboard

The web interface provides several views:

### 🏠 **Home Dashboard**
- Start new topic searches
- Real-time progress tracking
- Quick access to recent searches

### 📊 **Event Store**
- All tracked events with scores
- Alert history and patterns
- Cluster analysis and trends

### 📁 **Historical Browser**
- Previously searched topics
- Data export capabilities
- Timeline views

## 🛡️ Privacy & Security

- **Local Processing**: All AI analysis runs on your hardware
- **No Data Sharing**: Stories and analysis stay on your system
- **Configurable Sources**: Control which news feeds to monitor
- **Rate Limiting**: Respects news source rate limits

## 🚨 Troubleshooting

### Common Issues

**"URL environment variable not set"**
```bash
# Check your .env file
cat .env
# Should contain: URL=localhost
```

**"No news articles found"**
- Try broader search terms
- Check internet connectivity
- Verify Google News access

**LLM Processing Errors**
```bash
# Check if Ollama is running
curl http://localhost:11434/api/tags

# Pull the model if needed
ollama pull gemma2:2b
```

**Template Errors**
```bash
# Regenerate templates
python template_setup.py
```

### Performance Optimization

- Use smaller models for faster processing
- Adjust `cooldown_minutes` for your monitoring needs
- Clean up old data files periodically
- Monitor disk space usage

## 🤝 Contributing

We welcome contributions! Here are some areas where help is needed:

### 🌟 **Enhancement Ideas**
- Additional news source integrations (Reddit, Twitter, etc.)
- Advanced clustering algorithms (semantic similarity)
- Email/SMS alerting system
- Mobile-responsive improvements
- Database backend options (PostgreSQL, MongoDB)
- Docker containerization
- Kubernetes deployment manifests

### 🐛 **Bug Reports**
Please include:
- Operating system and Python version
- Ollama version and model used
- Complete error traceback
- Steps to reproduce

### 📝 **Documentation**
- Tutorial videos
- Use case examples
- API documentation
- Deployment guides

## 📊 Performance Metrics

Typical performance on modest hardware:

| Metric | Value |
|--------|-------|
| Articles/minute | 10-15 (with gemma2:2b) |
| Memory usage | 2-4GB (depending on model) |
| Storage growth | ~1MB per 100 articles |
| Response time | 1-3 seconds per article |

## 📄 License

This project is released under the MIT License. See [LICENSE](LICENSE) for details.

## 🙏 Acknowledgments

- [Ollama](https://ollama.com/) for local LLM infrastructure
- [Flask](https://flask.palletsprojects.com/) for the web framework
- [feedparser](https://pypi.org/project/feedparser/) for RSS processing
- The open source community for inspiration and tools

**⭐ If you find this project useful, please star it on GitHub!**

*Built with ❤️ for journalists, researchers, and anyone who needs to stay informed about rapidly evolving news events.*
