//get all the people sections
const people = Array.from(document.getElementsByClassName("person"));
let currentPerson = 0;

//add bullets to div
const bullets = document.getElementById("bullets");
loadBullets(); //adds all the bullets to the site
let currentBullet = document.getElementsByClassName("bullet")[currentPerson];
currentBullet.style.color="#fdc3ff";

for(let i = 1; i < people.length; i++) {
    people[i].style.display = "none";
}


//for pc
window.addEventListener("wheel", (e) => {
    if(e.deltaY > 0 && currentPerson < people.length-1){ //scrolls down the people array if current person isn't the last person and if you scroll down
        document.getElementsByClassName("bullet")[currentPerson].style.color="cadetblue"; //changes previous highlighted bullet back to normal
        switchPerson(true); //increments currentPerson and switches to next person
        
        currentBullet = document.getElementsByClassName("bullet")[currentPerson]; //increments currentbullet due to switchPerson()
        document.getElementsByClassName("bullet")[currentPerson].style.color="#fdc3ff"; //sets color of current bullet
    } 
    else if (e.deltaY < 0 && currentPerson != 0){ //scrolls down the people array if current person isn't the first person and if you scroll up
        document.getElementsByClassName("bullet")[currentPerson].style.color="cadetblue"; //changes previous highlighted bullet back to normal
        switchPerson(false); //increments currentPerson and switches to next person
        
        currentBullet = document.getElementsByClassName("bullet")[currentPerson]; //increments currentbullet due to switchPerson()
        document.getElementsByClassName("bullet")[currentPerson].style.color="#fdc3ff"; //sets color of current bullet
    }
}, { once: false });


//for tablet/phone
let touchStartY = 0;
window.addEventListener("touchstart", (e) => {
    touchStartY = e.touches[0].clientY;
});
window.addEventListener("touchend", (e) => {
    const touchEndY = e.changedTouches[0].clientY;
    if (touchStartY - touchEndY > 50 && currentPerson < people.length - 1) {
        document.getElementsByClassName("bullet")[currentPerson].style.color="cadetblue"; //changes previous highlighted bullet back to normal
        
        //swiped down
        switchPerson(true);
        currentBullet = document.getElementsByClassName("bullet")[currentPerson]; //increments currentbullet due to switchPerson()
        document.getElementsByClassName("bullet")[currentPerson].style.color="#fdc3ff"; //sets color of current bullet
    } else if (touchEndY - touchStartY > 50 && currentPerson != 0) {
        document.getElementsByClassName("bullet")[currentPerson].style.color="cadetblue"; //changes previous highlighted bullet back to normal
        
        //swiped up
        switchPerson(false);
        currentBullet = document.getElementsByClassName("bullet")[currentPerson]; //increments currentbullet due to switchPerson()
        document.getElementsByClassName("bullet")[currentPerson].style.color="#fdc3ff"; //sets color of current bullet
    }
});



function switchPerson(plusOrMinus){
        people[currentPerson].style.display="none";
        if(plusOrMinus){
            currentPerson++;
        }else{
            currentPerson--;
        }
        people[currentPerson].style.opacity = "0";
        people[currentPerson].style.display="flex";

        people[currentPerson].style.animation = "myAnimation 1s ease-in-out forwards";
        people[currentPerson].style.animationDelay = ".25s";
}

//adds each individual bullet needed to the 'bullets' div 
function loadBullets(){
    //const upArrow = document.createElement('p');
    //upArrow.textContent = '△';
    //bullets.appendChild(upArrow);
    for(let i = 0; i < people.length; i++){
        const singleBullet = document.createElement('p');
        singleBullet.textContent = '•'; //code to make a bullet
        singleBullet.classList.add("bullet");

        singleBullet.addEventListener("click", e => {
            people[currentPerson].style.display = "none"; //hide old person

            //change bullet color to new person from old person:
            document.getElementsByClassName("bullet")[currentPerson].style.color="cadetblue";
            currentPerson = i;
            currentBullet = document.getElementsByClassName("bullet")[i]; //update currentBullet
            currentBullet.style.color="#fdc3ff";

            //show new person:
            people[currentPerson].style.opacity = "0";
            people[currentPerson].style.display="flex";
            people[currentPerson].style.animation = "myAnimation 2s ease-in-out forwards";
            people[currentPerson].style.animationDelay = ".5s";

        });
        bullets.appendChild(singleBullet);
    }
    //const downArrow = document.createElement('p');
    //downArrow.textContent = '▽';
    //bullets.appendChild(downArrow);
}