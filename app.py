import os
import secrets
from datetime import datetime, date
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, render_template_string, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app=Flask(__name__)
app.config["SECRET_KEY"]=os.environ.get("SECRET_KEY","dev-only-change-before-deploy")
url=os.environ.get("DATABASE_URL","sqlite:///globmate.db")
if url.startswith("postgres://"): url=url.replace("postgres://","postgresql://",1)
app.config["SQLALCHEMY_DATABASE_URI"]=url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"]=False
db=SQLAlchemy(app)
CATEGORIES=["Travel & Tourism","Transport & Drivers","Home Services","Education & Training","Professional Services","Events & Experiences","Hotels & Accommodation","Beauty & Wellness","Repairs & Maintenance","Other Services"]

class User(db.Model):
 id=db.Column(db.Integer,primary_key=True); name=db.Column(db.String(120),nullable=False); email=db.Column(db.String(180),unique=True,nullable=False); password=db.Column(db.String(255),nullable=False); role=db.Column(db.String(20),nullable=False)
class Service(db.Model):
 id=db.Column(db.Integer,primary_key=True); provider_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=False); title=db.Column(db.String(180),nullable=False); category=db.Column(db.String(100),nullable=False); city=db.Column(db.String(100),nullable=False); description=db.Column(db.Text,nullable=False); price=db.Column(db.Float,nullable=False); status=db.Column(db.String(20),default="pending"); provider=db.relationship("User")
class Booking(db.Model):
 id=db.Column(db.Integer,primary_key=True); service_id=db.Column(db.Integer,db.ForeignKey("service.id"),nullable=False); customer_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=False); requested_date=db.Column(db.Date); message=db.Column(db.Text); status=db.Column(db.String(20),default="requested"); service=db.relationship("Service"); customer=db.relationship("User")

def current(): return db.session.get(User,session.get("uid")) if session.get("uid") else None
def login_required(fn):
 @wraps(fn)
 def w(*a,**k):
  if not current(): return redirect(url_for("login"))
  return fn(*a,**k)
 return w
def roles(*allowed):
 def deco(fn):
  @wraps(fn)
  @login_required
  def w(*a,**k):
   if current().role not in allowed: abort(403)
   return fn(*a,**k)
  return w
 return deco
BASE="""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{{title}} | GlobMate</title><link rel=stylesheet href=/static/style.css><header><a class=brand href='/'>Glob<span>Mate</span></a><nav><a href='/'>Explore</a>{% if u %}{% if u.role=='provider' %}<a href='/provider'>Provider</a>{% elif u.role=='admin' %}<a href='/admin'>Admin</a>{% else %}<a href='/customer'>My bookings</a>{% endif %}<a href='/logout'>Sign out</a>{% else %}<a href='/login'>Sign in</a><a class=navcta href='/register'>Join GlobMate</a>{% endif %}</nav></header><main>{% for c,m in get_flashed_messages(with_categories=true) %}<p class='flash {{c}}'>{{m}}</p>{% endfor %}{{body|safe}}</main><footer>© 2026 GlobMate · Discover services across India</footer></html>"""
def page(title,body,**ctx): return render_template_string(BASE,title=title,body=render_template_string(body,**ctx),u=current())
@app.get("/")
def home():
 q=request.args.get("q",""); city=request.args.get("city",""); cat=request.args.get("category","")
 query=Service.query.filter_by(status="approved")
 if q: query=query.filter(Service.title.ilike(f"%{q}%")|Service.description.ilike(f"%{q}%"))
 if city: query=query.filter(Service.city.ilike(f"%{city}%"))
 if cat: query=query.filter_by(category=cat)
 return page("Find services", """<section class=hero><p class=eyebrow>INDIA'S MULTI-SERVICE MARKETPLACE</p><h1>Find the right service.<br><em>Make it happen.</em></h1><p>Discover local professionals, experiences and services across India.</p></section><form class=search><input name=q placeholder='What service are you looking for?' value='{{q}}'><input name=city placeholder=City value='{{city}}'><select name=category><option value=''>All categories</option>{% for c in cats %}<option {% if c==cat %}selected{% endif %}>{{c}}</option>{% endfor %}</select><button>Search</button></form><h2>Explore services</h2><div class=cards>{% for s in services %}<a class=card href='/service/{{s.id}}'><small>{{s.category}}</small><h3>{{s.title}}</h3><p>{{s.description[:130]}}</p><b>{{s.city}} · ₹{{'%.2f'|format(s.price)}}</b></a>{% else %}<p>No approved listings yet. Providers can submit the first listing.</p>{% endfor %}</div>""",q=q,city=city,cat=cat,cats=CATEGORIES,services=query.order_by(Service.id.desc()).all())
@app.route("/register",methods=["GET","POST"])
def register():
 if request.method=="POST":
  name=request.form.get("name","").strip(); email=request.form.get("email","").strip().lower(); pw=request.form.get("password",""); role=request.form.get("role")
  if not name or not email or len(pw)<8 or role not in ("customer","provider"): flash("Enter valid details. Password must be at least 8 characters.","error")
  elif User.query.filter_by(email=email).first(): flash("Email already registered.","error")
  else:
   u=User(name=name,email=email,password=generate_password_hash(pw),role=role); db.session.add(u); db.session.commit(); session["uid"]=u.id; return redirect(url_for("provider") if role=="provider" else url_for("customer"))
 return page("Create account","""<div class=formbox><h1>Create your account</h1><form method=post><label>Full name<input name=name required></label><label>Email<input name=email type=email required></label><label>Password<input name=password type=password minlength=8 required></label><label>Account type<select name=role><option value=customer>Customer</option><option value=provider>Service Provider</option></select></label><button>Create account</button></form></div>""")
@app.route("/login",methods=["GET","POST"])
def login():
 if request.method=="POST":
  u=User.query.filter_by(email=request.form.get("email","").lower()).first()
  if u and check_password_hash(u.password,request.form.get("password","")):
   session.clear(); session["uid"]=u.id; return redirect(url_for("admin") if u.role=="admin" else url_for("provider") if u.role=="provider" else url_for("customer"))
  flash("Invalid email or password.","error")
 return page("Sign in","""<div class=formbox><h1>Welcome back</h1><form method=post><label>Email<input name=email type=email required></label><label>Password<input name=password type=password required></label><button>Sign in</button></form></div>""")
@app.get("/logout")
def logout(): session.clear(); return redirect(url_for("home"))

@app.route("/setup-super-admin",methods=["GET","POST"])
def setup_super_admin():
 setup_token=os.environ.get("ADMIN_SETUP_TOKEN","")
 admin_email=os.environ.get("ADMIN_EMAIL","").strip().lower()
 if not setup_token or not admin_email:
  abort(404)
 if User.query.filter_by(role="admin").first():
  abort(404)
 if request.method=="POST":
  supplied=request.form.get("setup_token","")
  name=request.form.get("name","GlobMate Super Admin").strip() or "GlobMate Super Admin"
  password=request.form.get("password","")
  if not secrets.compare_digest(supplied,setup_token):
   flash("Invalid setup token.","error")
  elif len(password)<12:
   flash("Choose a password with at least 12 characters.","error")
  elif User.query.filter_by(email=admin_email).first():
   flash("This email is already registered. Contact the site administrator.","error")
  else:
   user=User(name=name,email=admin_email,password=generate_password_hash(password),role="admin")
   db.session.add(user); db.session.commit()
   session.clear(); session["uid"]=user.id
   flash("Super Admin account created. Remove ADMIN_SETUP_TOKEN from Render Environment now.","success")
   return redirect(url_for("admin"))
 return page("Secure Super Admin setup","""<div class=formbox><h1>Create Super Admin</h1><p>This one-time setup is available only with the private setup token.</p><form method=post><label>Setup token<input name=setup_token type=password required autocomplete=off></label><label>Admin name<input name=name value='GlobMate Super Admin' required></label><label>Create admin password<input name=password type=password minlength=12 required autocomplete=new-password></label><button>Create Super Admin</button></form></div>""")

@app.route("/provider",methods=["GET","POST"])
@roles("provider")
def provider():
 u=current()
 if request.method=="POST":
  s=Service(provider_id=u.id,title=request.form["title"].strip(),category=request.form["category"],city=request.form["city"].strip(),description=request.form["description"].strip(),price=float(request.form["price"]),status="pending")
  db.session.add(s); db.session.commit(); flash("Listing submitted for admin approval.","success"); return redirect(url_for("provider"))
 body="""<h1>Provider workspace</h1><p>New listings stay pending until admin approval.</p><div class=panel><h2>Add a service</h2><form method=post><label>Service title<input name=title required></label><label>Category<select name=category>{% for c in cats %}<option>{{c}}</option>{% endfor %}</select></label><label>City<input name=city required></label><label>Price (INR)<input name=price type=number min=1 step=.01 required></label><label>Description<textarea name=description required></textarea></label><button>Submit for approval</button></form></div><h2>Your listings</h2><div class=cards>{% for s in services %}<div class=card><small>{{s.status|upper}}</small><h3>{{s.title}}</h3><p>{{s.category}} · {{s.city}}</p></div>{% else %}<p>No listings yet.</p>{% endfor %}</div><h2>Booking requests</h2><div class=cards>{% for b in bookings %}<div class=card><small>STATUS: {{b.status|upper}}</small><h3>{{b.service.title}}</h3><p>Customer: {{b.customer.name}} ({{b.customer.email}})</p><p>Requested date: {{b.requested_date or 'Not specified'}}</p><p>Message: {{b.message or 'No message'}}</p>{% if b.status=='requested' %}<form method=post action='/provider/booking/{{b.id}}/accepted'><button>Accept booking</button></form><form method=post action='/provider/booking/{{b.id}}/rejected'><button class=danger>Reject booking</button></form>{% endif %}</div>{% else %}<p>No booking requests yet.</p>{% endfor %}</div>"""
 bookings=Booking.query.join(Service,Booking.service_id==Service.id).filter(Service.provider_id==u.id).order_by(Booking.id.desc()).all()
 return page("Provider",body,cats=CATEGORIES,services=Service.query.filter_by(provider_id=u.id).all(),bookings=bookings)

@app.post("/provider/booking/<int:bid>/<status>")
@roles("provider")
def update_provider_booking(bid,status):
 if status not in ("accepted","rejected"): abort(400)
 booking=db.session.get(Booking,bid) or abort(404)
 if booking.service.provider_id != current().id: abort(403)
 if booking.status != "requested":
  flash("This booking has already been processed.","error")
 else:
  booking.status=status
  db.session.commit()
  flash("Booking "+status+".","success")
 return redirect(url_for("provider"))
@app.get("/service/<int:sid>")
def service(sid):
 s=Service.query.filter_by(id=sid,status="approved").first_or_404()
 return page(s.title,"""<div class=panel><small>{{s.category}} · {{s.city}}</small><h1>{{s.title}}</h1><p>{{s.description}}</p><h2>₹{{'%.2f'|format(s.price)}}</h2>{% if u and u.role=='customer' %}<form method=post action='/book/{{s.id}}'><label>Preferred date<input type=date name=day></label><label>Message<textarea name=message></textarea></label><button>Request booking</button></form>{% elif not u %}<a class=button href='/login'>Sign in to book</a>{% endif %}</div>""",s=s)
@app.post("/book/<int:sid>")
@roles("customer")
def book(sid):
 s=Service.query.filter_by(id=sid,status="approved").first_or_404(); d=request.form.get("day") or None
 b=Booking(service=s,customer=current(),requested_date=date.fromisoformat(d) if d else None,message=request.form.get("message","")); db.session.add(b); db.session.commit(); flash("Booking request sent.","success"); return redirect(url_for("customer"))
@app.get("/customer")
@roles("customer")
def customer():
 bs=Booking.query.filter_by(customer_id=current().id).order_by(Booking.id.desc()).all()
 return page("My bookings","""<h1>My booking requests</h1><div class=cards>{% for b in bookings %}<div class=card><small>{{b.status|upper}}</small><h3>{{b.service.title}}</h3><p>{{b.service.city}} · {{b.requested_date or 'Date not set'}}</p></div>{% else %}<p>No bookings yet. <a href='/'>Explore services</a>.</p>{% endfor %}</div>""",bookings=bs)
@app.route("/admin/account-lookup",methods=["GET","POST"])
@roles("admin")
def admin_account_lookup():
 account=None
 searched_email=""
 if request.method=="POST":
  searched_email=request.form.get("email","").strip().lower()
  if searched_email:
   account=User.query.filter_by(email=searched_email).first()
 return page("Provider account lookup","""<div class=formbox><h1>Account lookup</h1><p>Search by exact email. This tool only displays account name, email and role; it never displays passwords.</p><form method=post><label>Email address<input name=email type=email value='{{searched_email}}' required></label><button>Check account</button></form>{% if searched_email %}{% if account %}<div class=panel><h2>Account found</h2><p>Name: {{account.name}}</p><p>Email: {{account.email}}</p><p>Role: {{account.role}}</p><p>Account ID: {{account.id}}</p></div>{% else %}<div class=panel><h2>No account found</h2><p>No user is registered with {{searched_email}}.</p></div>{% endif %}{% endif %}<p><a href='/admin'>Back to Admin</a></p></div>""",account=account,searched_email=searched_email)

@app.get("/admin")
@roles("admin")
def admin():
 pending=Service.query.filter_by(status="pending").all()
 return page("Admin","""<h1>Super Admin control centre</h1><div class=panel><h2>Provider account check</h2><p>Verify whether an account exists and confirm its role.</p><a class=button href='/admin/account-lookup'>Check account by email</a></div><h2>Pending provider listings</h2><div class=cards>{% for s in pending %}<div class=card><small>{{s.category}} · {{s.city}}</small><h3>{{s.title}}</h3><p>{{s.description}}</p><p>Provider: {{s.provider.name}} ({{s.provider.email}})</p><form method=post action='/admin/review/{{s.id}}/approved'><button>Approve</button></form><form method=post action='/admin/review/{{s.id}}/rejected'><button class=danger>Reject</button></form></div>{% else %}<p>No pending listings yet.</p>{% endfor %}</div>""",pending=pending)
@app.post("/admin/review/<int:sid>/<status>")
@roles("admin")
def review(sid,status):
 if status not in ("approved","rejected"): abort(400)
 s=db.session.get(Service,sid) or abort(404); s.status=status; db.session.commit(); flash("Listing "+status+".","success"); return redirect(url_for("admin"))
@app.cli.command("init-db")
def init_db():
 db.create_all(); print("GlobMate database tables created.")
@app.cli.command("create-admin")
def create_admin():
 email=os.environ.get("ADMIN_EMAIL"); password=os.environ.get("ADMIN_PASSWORD"); name=os.environ.get("ADMIN_NAME","GlobMate Super Admin")
 if not email or not password: raise SystemExit("Set ADMIN_EMAIL and ADMIN_PASSWORD")
 db.create_all()
 if User.query.filter_by(email=email.lower()).first(): print("Admin already exists."); return
 db.session.add(User(name=name,email=email.lower(),password=generate_password_hash(password),role="admin")); db.session.commit(); print("Super Admin created.")
with app.app_context():
 db.create_all()
 # Keep the configured Super Admin account synchronized with Render environment values.
 admin_email=os.environ.get("ADMIN_EMAIL","").strip().lower()
 admin_password=os.environ.get("ADMIN_PASSWORD","")
 if admin_email and admin_password:
  admin_user=User.query.filter_by(email=admin_email).first()
  if admin_user:
   admin_user.name=os.environ.get("ADMIN_NAME","GlobMate Super Admin")
   admin_user.role="admin"
   admin_user.password=generate_password_hash(admin_password)
  else:
   db.session.add(User(name=os.environ.get("ADMIN_NAME","GlobMate Super Admin"),email=admin_email,password=generate_password_hash(admin_password),role="admin"))
  db.session.commit()
if __name__=="__main__": app.run(debug=True)
